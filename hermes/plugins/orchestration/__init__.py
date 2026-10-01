"""hermes-orchestrator plugin for official Hermes (ARCHITECTURE section 7.1, docs/design/phase-9.md).

LLM tools          read and create only (AD-10): a prompt-injected agent cannot approve, cancel, or
                   change budgets.
/orch              human actions from chat. The principal is the authenticated sender of the message:
                   the gateway authorizes the user first and binds HERMES_SESSION_PLATFORM/USER_ID for
                   plugin command handlers (gateway/run_inbound.py at the pinned release).
hermes orchestration ...   the same verbs from the host CLI.

The control plane decides what each principal may do (approvals need `platform.approvers`).
"""

from __future__ import annotations

import getpass
import json
import shlex
from typing import Any

from .orch_client import ApiError, TaskApi, approval_line, task_line, task_summary

AGENT_PRINCIPAL = "hermes:agent"
HELP = """/orch commands:
  tasks [project]                list active tasks
  status <T-n>                   task status from the orchestrator
  create <project> <request...>  create a task
  approvals                      pending approvals
  approve <id> | reject <id> [note]
  pause|resume|cancel|retry <T-n>
  revise <T-n> <new requirements...>
  budget <T-n> [counter=N ...]   show, or request an increase
  projects                       registered projects"""


# ---------------------------------------------------------------- principals


def session_principal() -> str | None:
    """`<platform>:<user id>` of the message being handled, or None outside a gateway message."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return None
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    user = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    if not platform or not user:
        return None
    return f"{platform}:{user}"


def cli_principal() -> str:
    return f"hermes-cli:{getpass.getuser()}"


# ---------------------------------------------------------------- verbs


def run_verb(api: TaskApi, words: list[str], *, human: bool) -> str:
    """Execute one /orch or CLI verb; returns the text to show."""
    if not words or words[0] in ("help", "-h", "--help"):
        return HELP
    verb, args = words[0].lower(), words[1:]
    if verb == "tasks":
        result = api.tasks(project=args[0] if args else None)
        tasks = [t for t in result.get("tasks", []) if t["state"] not in ("DONE", "CANCELLED", "FAILED")]
        return "\n".join(task_line(t) for t in tasks) or "No active tasks."
    if verb == "status":
        _need(args, 1, "status <T-n>")
        return task_summary(api.task(args[0]))
    if verb == "projects":
        projects = api.projects().get("projects", [])
        return "\n".join(f"{p['slug']} [{p['status']}]" for p in projects) or "No projects registered."
    if verb == "approvals":
        approvals = api.approvals().get("approvals", [])
        return "\n".join(approval_line(a) for a in approvals) or "No pending approvals."
    if verb == "create":
        _need(args, 2, "create <project> <request...>")
        task = api.create(args[0], " ".join(args[1:]))
        return f"Created {task['key']} in {args[0]} [{task['state']}]."
    if not human:
        raise ApiError(403, f"'{verb}' is a human action; it needs the identity of the person asking")
    if verb in ("approve", "reject"):
        _need(args, 1, f"{verb} <approval id> [note]")
        approval = api.decide(args[0], verb == "approve", " ".join(args[1:]) or None)
        return f"{approval['action']} {approval['state'].lower()}: {approval.get('summary') or ''}".strip()
    if verb in ("pause", "resume", "cancel", "retry"):
        _need(args, 1, f"{verb} <T-n>")
        task = api.action(args[0], verb)
        return f"{task['key']} is now {task['state']}."
    if verb == "revise":
        _need(args, 2, "revise <T-n> <new requirements...>")
        result = api.revise(args[0], " ".join(args[1:]))
        return f"{result['task']} requirements {result['requirements']}; the orchestrator will analyse the impact."
    if verb == "budget":
        _need(args, 1, "budget <T-n> [counter=N ...]")
        add = {}
        for item in args[1:]:
            counter, _, amount = item.partition("=")
            if not amount.isdigit():
                raise ApiError(400, f"budget increases look like agent_launches=5, not {item!r}")
            add[counter] = int(amount)
        if add:
            approval = api.budget(args[0], add)
            return f"Budget increase requested ({approval['id']}); it needs an approval: /orch approve {approval['id']}"
        budget = api.budget(args[0])
        return f"{args[0].upper()} budget {budget.get('state')}: consumed {json.dumps(budget.get('consumed'))}"
    raise ApiError(400, f"unknown command '{verb}'\n\n{HELP}")


def _need(args: list[str], count: int, usage: str) -> None:
    if len(args) < count:
        raise ApiError(400, f"usage: /orch {usage}")


def handle_slash(raw_args: str) -> str:
    principal = session_principal()
    try:
        words = shlex.split(raw_args or "")
    except ValueError as exc:
        return f"Could not parse the command: {exc}"
    try:
        return run_verb(TaskApi(principal or AGENT_PRINCIPAL), words, human=principal is not None)
    except ApiError as exc:
        return _error(exc)


def _error(exc: ApiError) -> str:
    if exc.status == 403:
        return f"Not allowed: {exc.message}"
    if exc.status == 0:
        return f"The orchestrator is unavailable right now ({exc.message}). Your request was not applied."
    return f"Error: {exc.message}"


# ---------------------------------------------------------------- LLM tools (read and create only)


def _tool(fn):
    def handler(args: dict, **_kw: Any) -> str:
        try:
            return json.dumps(fn(TaskApi(session_principal() or AGENT_PRINCIPAL), args or {}), default=str)
        except ApiError as exc:
            return json.dumps({"error": exc.message, "status": exc.status})
    return handler


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_STR = {"type": "string"}
TOOLS = (
    ("orch_task_create",
     _schema("orch_task_create", "Create a development task in a registered hermes-orchestrator project. The orchestrator "
             "plans it, Claude and Codex implement and cross-review it, and it waits for a human merge approval.",
             {"project": {**_STR, "description": "project slug"}, "request": {**_STR, "description": "what to build or fix"},
              "title": _STR, "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]}},
             ["project", "request"]),
     lambda api, a: api.create(a["project"], a["request"], a.get("title"), a.get("priority"))),
    ("orch_task_status",
     _schema("orch_task_status", "Current state of a hermes-orchestrator task, from persistent state.",
             {"task": {**_STR, "description": "task key, for example T-12"}}, ["task"]),
     lambda api, a: api.task(a["task"])),
    ("orch_task_list",
     _schema("orch_task_list", "List hermes-orchestrator tasks, optionally for one project or state.",
             {"project": _STR, "state": _STR}, []),
     lambda api, a: api.tasks(a.get("project"), a.get("state"))),
    ("orch_task_inspect",
     _schema("orch_task_inspect", "Plan, subtasks, recent orchestrator actions, and budget of a task.",
             {"task": _STR}, ["task"]),
     lambda api, a: api.inspect(a["task"])),
    ("orch_project_list",
     _schema("orch_project_list", "Projects registered in hermes-orchestrator.", {}, []),
     lambda api, a: api.projects()),
    ("orch_approvals_list",
     _schema("orch_approvals_list", "Pending approvals (read-only; deciding them is a human action through /orch).", {}, []),
     lambda api, a: api.approvals()),
)


# ---------------------------------------------------------------- CLI


def _cli_setup(parser) -> None:
    parser.add_argument("words", nargs="*", help="verb and arguments, as for /orch (try: help)")


def _cli_run(args) -> None:
    try:
        print(run_verb(TaskApi(cli_principal()), list(args.words or []), human=True))
    except ApiError as exc:
        print(_error(exc))
        raise SystemExit(1) from exc


# ---------------------------------------------------------------- registration


def register(ctx) -> None:
    for name, schema, fn in TOOLS:
        ctx.register_tool(name=name, toolset="orchestration", schema=schema, handler=_tool(fn),
                          description=schema["description"])
    ctx.register_command("orch", handler=handle_slash, description="hermes-orchestrator tasks and approvals",
                         args_hint="<command>")
    ctx.register_cli_command("orchestration", help="hermes-orchestrator tasks and approvals",
                             setup_fn=_cli_setup, handler_fn=_cli_run)
