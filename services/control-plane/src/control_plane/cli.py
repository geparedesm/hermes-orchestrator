"""Host operator CLI (`ho`), run inside the control-plane container:

    docker compose exec control-plane ho project register /Users/me/HermesProjects/my-app
    docker compose exec control-plane ho approval list

It calls the Task API on localhost with the operator token, so it acts as the
principal host-cli:operator. The Hermes CLI integration (`hermes orchestration
...`) arrives in Phase 9 and uses the same API.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx


def _client() -> httpx.Client:
    token_file = os.environ.get("HO_OPERATOR_TOKEN_FILE", "/run/secrets/ho_operator_token")
    token = Path(token_file).read_text().strip()
    base = os.environ.get("HO_API_URL", f"http://127.0.0.1:{os.environ.get('HO_PORT', '8080')}")
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=120)


def _call(method: str, path: str, *, body: Any = None, params: dict[str, Any] | None = None, mutate: bool = False) -> int:
    headers = {"Idempotency-Key": f"cli-{uuid.uuid4()}"} if mutate else {}
    with _client() as client:
        response = client.request(method, path, json=body, params=params, headers=headers)
    try:
        payload = response.json()
    except ValueError:
        payload = {"status": response.status_code, "body": response.text}
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if response.status_code < 400 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ho", description="Hermes Orchestrator operator CLI")
    sub = parser.add_subparsers(dest="group", required=True)

    project = sub.add_parser("project").add_subparsers(dest="cmd", required=True)
    p = project.add_parser("register", help="register a repository inside the projects root")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--slug")
    project.add_parser("list")
    project.add_parser("show").add_argument("slug")
    project.add_parser("scan", help="read-only onboarding scan and configuration proposal").add_argument("slug")
    project.add_parser("unregister", help="remove from the registry; never deletes files").add_argument("slug")

    task = sub.add_parser("task").add_subparsers(dest="cmd", required=True)
    t = task.add_parser("create")
    t.add_argument("project")
    t.add_argument("request")
    t.add_argument("--title")
    t.add_argument("--priority", choices=["CRITICAL", "HIGH", "NORMAL", "LOW"])
    t.add_argument("--budget", choices=["SMALL", "NORMAL", "LARGE", "UNLIMITED"])
    t.add_argument("--depends-on", action="append", default=[])
    t.add_argument("--attach", action="append", default=[], metavar="PATH",
                   help="attach a file readable inside this container (repeatable)")
    t.add_argument("--attach-stdin", metavar="NAME",
                   help="attach standard input as NAME (from the host: ... --attach-stdin design.png < design.png)")
    lst = task.add_parser("list")
    lst.add_argument("--project")
    lst.add_argument("--state")
    for name in ("show", "pause", "resume", "cancel", "retry", "events"):
        task.add_parser(name).add_argument("key")
    task.add_parser("queue")
    rv = task.add_parser("revise", help="new requirements version; the orchestrator analyses the impact")
    rv.add_argument("key")
    rv.add_argument("text")
    bd = task.add_parser("budget", help="show the budget, or request an increase (--add agent_launches=5)")
    bd.add_argument("key")
    bd.add_argument("--add", action="append", default=[], metavar="COUNTER=N")
    ins = task.add_parser("inspect", help="orchestration state: lease, plan, actions, budget")
    ins.add_argument("key")
    ins.add_argument("--manifest", action="store_true", help="generate an on-demand Task Manifest")

    ins_cp = task.add_parser("checkpoints", help="durable snapshots of a task's state")
    ins_cp.add_argument("key")
    update = sub.add_parser("update", help="approval-controlled platform updates (scripts/update.sh)").add_subparsers(dest="cmd", required=True)
    update.add_parser("list")
    update.add_parser("request").add_argument("to_version")
    us = update.add_parser("start")
    us.add_argument("approval")
    us.add_argument("--backup")
    uf = update.add_parser("finish")
    uf.add_argument("update")
    uf.add_argument("state", choices=["SUCCEEDED", "ROLLED_BACK", "FAILED"])
    uf.add_argument("--note", default="")
    notes = sub.add_parser("notifications", help="chat notifications (operator)").add_subparsers(dest="cmd", required=True)
    sup = notes.add_parser("suppress", help="withdraw pending notifications (kept as SUPPRESSED, audited)")
    sup.add_argument("--before", help="only those created before this ISO time (default: all pending now)")
    notes.add_parser("test", help="send a test notification to the configured chat")
    sub.add_parser("maintenance", help="daily maintenance: caches, retention (operator)").add_subparsers(dest="cmd", required=True).add_parser("run")
    cache = sub.add_parser("cache", help="dependency caches per project and ecosystem").add_subparsers(dest="cmd", required=True)
    cache.add_parser("list")
    clear = cache.add_parser("clear")
    clear.add_argument("project")
    clear.add_argument("--ecosystem", choices=["pip", "npm", "gradle", "pub", "composer"])
    recovery = sub.add_parser("recovery", help="reconciliation runs and platform health").add_subparsers(dest="cmd", required=True)
    recovery.add_parser("status")
    recovery.add_parser("run", help="reconcile now (operator)")

    knowledge = sub.add_parser("knowledge", help="project knowledge proposed by agents").add_subparsers(dest="cmd", required=True)
    knowledge.add_parser("list").add_argument("project")
    for name in ("confirm", "reject"):
        knowledge.add_parser(name).add_argument("id")

    approval = sub.add_parser("approval").add_subparsers(dest="cmd", required=True)
    approval.add_parser("list").add_argument("--all", action="store_true")
    approval.add_parser("show").add_argument("id")
    for name in ("approve", "reject"):
        a = approval.add_parser(name)
        a.add_argument("id")
        a.add_argument("--note")

    def execution_options(e: argparse.ArgumentParser, *, default_role: str) -> None:
        e.add_argument("--role", default=default_role, choices=["ORCHESTRATOR", "DEVELOPER", "REVIEWER", "TESTER", "BROWSER"])
        e.add_argument("--provider", choices=["claude", "codex"])
        e.add_argument("--workspace", help=".hermes/worktrees/<name> inside the project")
        e.add_argument("--workspace-access", default="NONE", choices=["NONE", "READ", "WRITE"])
        e.add_argument("--egress", default="NONE", choices=["NONE", "PROVIDER_ONLY", "ALLOWLIST", "STANDARD"])
        e.add_argument("--allow-domain", action="append", default=[])
        e.add_argument("--test-services", action="store_true")
        e.add_argument("--secret", action="append", default=[], help="secret NAME from .hermes/project.yaml (repeatable)")
        e.add_argument("--profile", choices=["LIGHT", "NORMAL", "HEAVY"])
        e.add_argument("--timeout", type=int, default=30, help="minutes")

    agent = sub.add_parser("agent", help="run Claude Code or Codex on an assignment").add_subparsers(dest="cmd", required=True)
    a = agent.add_parser("run", help="start an agent execution for a task")
    a.add_argument("task")
    a.add_argument("prompt", help="the assignment")
    execution_options(a, default_role="DEVELOPER")
    a.add_argument("--max-turns", type=int, default=60)
    a.add_argument("--model")

    execution = sub.add_parser("execution", help="executions (agent runs and raw commands)").add_subparsers(dest="cmd", required=True)
    e = execution.add_parser("run", help="run a raw command in an isolated worker")
    e.add_argument("task")
    e.add_argument("command", help="shell command run with bash -c inside the worker")
    execution_options(e, default_role="TESTER")
    r = execution.add_parser("resume", help="continue an agent execution's provider session")
    r.add_argument("id")
    r.add_argument("prompt")
    el = execution.add_parser("list")
    el.add_argument("--task")
    el.add_argument("--state")
    execution.add_parser("show").add_argument("id")
    execution.add_parser("stop").add_argument("id")
    execution.add_parser("replace", help="stop an execution and start a fresh one with the same request").add_argument("id")
    sub.add_parser("workers", help="Agent Manager capacity and managed containers")

    git = sub.add_parser("git", help="task workspaces, integration, pull requests, and merge requests").add_subparsers(dest="cmd", required=True)
    w = git.add_parser("workspace", help="create an isolated clone and branch for a task")
    w.add_argument("task")
    w.add_argument("--suffix", help="workspace suffix (default w1, w2, ...)")
    git.add_parser("status", help="workspaces, integration, divergence, PR, and merge state").add_argument("task")
    for name, text in (("collect", "read commits from the task's workspaces"),
                       ("divergence", "classify human changes on the target since the task's base"),
                       ("integrate", "merge collected work onto the current target and retest it"),
                       ("push", "push the integrated change to the task's branch (GitHub)"),
                       ("pr", "create or update the task's pull request (GitHub)"),
                       ("checks", "CI status of the task's pull request"),
                       ("merge-request", "request the human MERGE approval (task must be READY_FOR_MERGE)")):
        git.add_parser(name, help=text).add_argument("task")
    rs = git.add_parser("resolve", help="let an agent resolve an integration conflict")
    rs.add_argument("task")
    rs.add_argument("--provider", required=True, choices=["claude", "codex"])

    sub.add_parser("verify", help="run the full verification of a task's integrated change").add_argument("task")
    tests = sub.add_parser("tests", help="verification runs and test results").add_subparsers(dest="cmd", required=True)
    tests.add_parser("show").add_argument("task")
    review = sub.add_parser("review", help="cross-review by a provider other than the developers").add_subparsers(dest="cmd", required=True)
    rr = review.add_parser("run")
    rr.add_argument("task")
    rr.add_argument("--provider", required=True, choices=["claude", "codex"])
    review.add_parser("show").add_argument("task")
    gate = sub.add_parser("gate", help="Quality Gate").add_subparsers(dest="cmd", required=True)
    gate.add_parser("evaluate", help="evaluate the Quality Gate now").add_argument("task")
    gate.add_parser("show", help="latest evaluation").add_argument("task")

    auth = sub.add_parser("auth", help="provider logins (Credential Broker)").add_subparsers(dest="cmd", required=True)
    auth.add_parser("status", help="provider health and credential status")
    ready = auth.add_parser("ready", help="confirm a new login (after make auth-<provider>) and resume waiting tasks")
    ready.add_argument("provider", choices=["claude", "codex"])
    ready.add_argument("--identity", default="default")

    policy = sub.add_parser("policy").add_subparsers(dest="cmd", required=True)
    c = policy.add_parser("check", help="classify a command")
    c.add_argument("command")
    c.add_argument("--autonomy", default="BALANCED", choices=["SUPERVISED", "BALANCED", "AUTONOMOUS"])

    sub.add_parser("health")

    args = parser.parse_args(argv)
    g, cmd = args.group, getattr(args, "cmd", None)

    if g == "health":
        return _call("GET", "/health/ready")
    if g == "project":
        if cmd == "register":
            return _call("POST", "/v1/projects", body={"path": args.path, "name": args.name, "slug": args.slug}, mutate=True)
        if cmd == "list":
            return _call("GET", "/v1/projects")
        if cmd == "show":
            return _call("GET", f"/v1/projects/{args.slug}")
        if cmd == "scan":
            return _call("POST", f"/v1/projects/{args.slug}/scan", mutate=True)
        if cmd == "unregister":
            return _call("DELETE", f"/v1/projects/{args.slug}", mutate=True)
    if g == "task":
        if cmd == "create":
            body: dict[str, Any] = {"project": args.project, "request": args.request}
            if args.title:
                body["title"] = args.title
            if args.priority:
                body["priority"] = args.priority
            if args.budget:
                body["budget_profile"] = args.budget
            if args.depends_on:
                body["related_tasks"] = [{"task": k, "kind": "DEPENDENCY"} for k in args.depends_on]
            files = [(Path(p).name, Path(p).read_bytes()) for p in args.attach]
            if args.attach_stdin:
                files.append((args.attach_stdin, sys.stdin.buffer.read()))
            if files:
                body["attachments"] = [{"name": n, "content_base64": base64.b64encode(c).decode()} for n, c in files]
            return _call("POST", "/v1/tasks", body=body, mutate=True)
        if cmd == "list":
            return _call("GET", "/v1/tasks", params={k: v for k, v in (("project", args.project), ("state", args.state)) if v})
        if cmd == "show":
            return _call("GET", f"/v1/tasks/{args.key}")
        if cmd == "events":
            return _call("GET", "/v1/events", params={"task": args.key})
        if cmd == "queue":
            return _call("GET", "/v1/queue")
        if cmd == "revise":
            return _call("POST", f"/v1/tasks/{args.key}/revise", body={"text": args.text}, mutate=True)
        if cmd == "budget":
            if not args.add:
                return _call("GET", f"/v1/tasks/{args.key}/budget")
            add = {}
            for item in args.add:
                counter, _, amount = item.partition("=")
                if not amount.isdigit():
                    parser.error(f"--add expects COUNTER=N, got {item!r}")
                add[counter] = int(amount)
            return _call("POST", f"/v1/tasks/{args.key}/budget", body={"add": add}, mutate=True)
        if cmd == "checkpoints":
            return _call("GET", f"/v1/tasks/{args.key}/checkpoints")
        if cmd == "inspect":
            if args.manifest:
                return _call("POST", f"/v1/tasks/{args.key}/manifest", mutate=True)
            return _call("GET", f"/v1/tasks/{args.key}/orchestration")
        return _call("POST", f"/v1/tasks/{args.key}/{cmd}", mutate=True)
    if g == "approval":
        if cmd == "list":
            return _call("GET", "/v1/approvals", params={"state": "" if args.all else "PENDING"})
        if cmd == "show":
            return _call("GET", f"/v1/approvals/{args.id}")
        decision = "APPROVE" if cmd == "approve" else "REJECT"
        return _call("POST", f"/v1/approvals/{args.id}/decision", body={"decision": decision, "note": args.note}, mutate=True)
    def execution_body(extra: dict[str, Any]) -> dict[str, Any]:
        caps: dict[str, Any] = {"workspace": args.workspace_access, "egress": args.egress,
                                "test_services": args.test_services, "tests": "EXECUTE", "artifacts": "WRITE"}
        if args.workspace_access == "WRITE":
            caps["git"] = "LOCAL_COMMIT"
        elif args.workspace_access == "READ":
            caps["git"] = "READ"
        if args.allow_domain:
            caps["allowed_domains"] = args.allow_domain
        return {"role": args.role, "provider": args.provider, "workspace": args.workspace, "capabilities": caps,
                "resource_profile": args.profile, "timeout_minutes": args.timeout, "secrets": args.secret, **extra}

    if g == "agent":
        body = execution_body({"prompt": args.prompt, "max_turns": args.max_turns, "model": args.model})
        return _call("POST", f"/v1/tasks/{args.task}/executions", body=body, mutate=True)
    if g == "git":
        if cmd == "workspace":
            return _call("POST", f"/v1/tasks/{args.task}/git/workspaces", body={"suffix": args.suffix}, mutate=True)
        if cmd == "status":
            return _call("GET", f"/v1/tasks/{args.task}/git")
        if cmd == "resolve":
            return _call("POST", f"/v1/tasks/{args.task}/git/resolve", body={"provider": args.provider}, mutate=True)
        return _call("POST", f"/v1/tasks/{args.task}/git/{cmd}", mutate=True)
    if g == "verify":
        return _call("POST", f"/v1/tasks/{args.task}/verify", mutate=True)
    if g == "tests":
        return _call("GET", f"/v1/tasks/{args.task}/tests")
    if g == "review":
        if cmd == "run":
            return _call("POST", f"/v1/tasks/{args.task}/reviews", body={"provider": args.provider}, mutate=True)
        return _call("GET", f"/v1/tasks/{args.task}/reviews")
    if g == "gate":
        if cmd == "evaluate":
            return _call("POST", f"/v1/tasks/{args.task}/quality-gate", mutate=True)
        return _call("GET", f"/v1/tasks/{args.task}/quality-gate")
    if g == "auth":
        if cmd == "status":
            return _call("GET", "/v1/credentials")
        return _call("POST", f"/v1/credentials/{args.provider}/{args.identity}/ready", mutate=True)
    if g == "execution":
        if cmd == "run":
            body = execution_body({"command": ["bash", "-c", args.command]})
            return _call("POST", f"/v1/tasks/{args.task}/executions", body=body, mutate=True)
        if cmd == "resume":
            return _call("POST", f"/v1/executions/{args.id}/resume", body={"prompt": args.prompt}, mutate=True)
        if cmd == "list":
            return _call("GET", "/v1/executions", params={k: v for k, v in (("task", args.task), ("state", args.state)) if v})
        if cmd == "show":
            return _call("GET", f"/v1/executions/{args.id}")
        if cmd == "replace":
            return _call("POST", f"/v1/executions/{args.id}/replace", body={"reason": "replaced by operator"}, mutate=True)
        return _call("POST", f"/v1/executions/{args.id}/stop", body={"reason": "stopped by operator"}, mutate=True)
    if g == "update":
        if cmd == "list":
            return _call("GET", "/v1/platform/updates")
        if cmd == "request":
            return _call("POST", "/v1/platform/updates", body={"to_version": args.to_version}, mutate=True)
        if cmd == "start":
            return _call("POST", f"/v1/platform/updates/{args.approval}/start", body={"backup": args.backup}, mutate=True)
        return _call("POST", f"/v1/platform/updates/runs/{args.update}/finish",
                     body={"state": args.state, "report": {"note": args.note}}, mutate=True)
    if g == "notifications":
        if cmd == "suppress":
            return _call("POST", "/v1/notifications/suppress", body={"before": args.before} if args.before else {}, mutate=True)
        return _call("POST", "/v1/notifications/test", mutate=True)
    if g == "maintenance":
        return _call("POST", "/v1/maintenance/run", mutate=True)
    if g == "cache":
        if cmd == "list":
            return _call("GET", "/v1/caches")
        return _call("DELETE", f"/v1/caches/{args.project}", params={"ecosystem": args.ecosystem} if args.ecosystem else None,
                     mutate=True)
    if g == "recovery":
        if cmd == "run":
            return _call("POST", "/v1/recovery/run", mutate=True)
        return _call("GET", "/v1/recovery")
    if g == "knowledge":
        if cmd == "list":
            return _call("GET", f"/v1/projects/{args.project}/knowledge")
        return _call("POST", f"/v1/knowledge/{args.id}/decision", body={"decision": cmd.upper()}, mutate=True)
    if g == "workers":
        return _call("GET", "/v1/workers")
    if g == "policy":
        return _call("POST", "/v1/policy/commands/evaluate", body={"command": args.command, "autonomy": args.autonomy})
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
