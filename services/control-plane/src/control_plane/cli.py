"""Host operator CLI (`ho`), run inside the control-plane container:

    docker compose exec control-plane ho project register /Users/me/HermesProjects/my-app
    docker compose exec control-plane ho approval list

It calls the Task API on localhost with the operator token, so it acts as the
principal host-cli:operator. The Hermes CLI integration (`hermes orchestration
...`) arrives in Phase 9 and uses the same API.
"""

from __future__ import annotations

import argparse
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
    lst = task.add_parser("list")
    lst.add_argument("--project")
    lst.add_argument("--state")
    for name in ("show", "pause", "resume", "cancel", "retry", "events"):
        task.add_parser(name).add_argument("key")
    task.add_parser("queue")

    approval = sub.add_parser("approval").add_subparsers(dest="cmd", required=True)
    approval.add_parser("list").add_argument("--all", action="store_true")
    approval.add_parser("show").add_argument("id")
    for name in ("approve", "reject"):
        a = approval.add_parser(name)
        a.add_argument("id")
        a.add_argument("--note")

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
            return _call("POST", "/v1/tasks", body=body, mutate=True)
        if cmd == "list":
            return _call("GET", "/v1/tasks", params={k: v for k, v in (("project", args.project), ("state", args.state)) if v})
        if cmd == "show":
            return _call("GET", f"/v1/tasks/{args.key}")
        if cmd == "events":
            return _call("GET", "/v1/events", params={"task": args.key})
        if cmd == "queue":
            return _call("GET", "/v1/queue")
        return _call("POST", f"/v1/tasks/{args.key}/{cmd}", mutate=True)
    if g == "approval":
        if cmd == "list":
            return _call("GET", "/v1/approvals", params={"state": "" if args.all else "PENDING"})
        if cmd == "show":
            return _call("GET", f"/v1/approvals/{args.id}")
        decision = "APPROVE" if cmd == "approve" else "REJECT"
        return _call("POST", f"/v1/approvals/{args.id}/decision", body={"decision": decision, "note": args.note}, mutate=True)
    if g == "policy":
        return _call("POST", "/v1/policy/commands/evaluate", body={"command": args.command, "autonomy": args.autonomy})
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
