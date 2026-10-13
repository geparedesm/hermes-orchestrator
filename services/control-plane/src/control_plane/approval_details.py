"""What a pending approval is about, for the person deciding it. Two levels: a plain `headline` (one sentence a
non-developer can read), up to three `highlights`, and a `recommendation` computed from fixed rules (never by a
model); then the technical details behind them: what changes, why it needs a person, what yes and no do, and the
facts (Dashboard cards; the headline and first changes also go into chat notifications).

Read-only and computed on request from the approval's subject and the current state. A failure to describe an
approval never hides it: `details` falls back to the approval's own summary.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import budgets
from .context import UnitOfWork
from .db import Row

log = logging.getLogger(__name__)
MAX_CHANGES = 40

COUNTERS = {"runtime_minutes": "working time (min)", "agent_launches": "agent runs", "retries": "retries",
            "review_cycles": "review rounds", "subtasks": "subtasks", "provider_usage_units": "usage units"}
SECTIONS = {"toolchain": "Toolchain", "commands": "Command", "test_environment": "Test services", "quality_gate": "Quality gate",
            "network": "Network", "secrets": "Secrets", "environments": "Environments", "agents": "Agents",
            "autonomy": "Autonomy", "budget": "Budget", "git": "Git", "verification": "Verification", "review": "Review",
            "retries": "Retries", "resources": "Resources", "expansion": "Plan expansion", "browser_tests": "Browser tests",
            "knowledge": "Knowledge", "config_drift": "Configuration drift", "retention": "Retention", "project": "Project"}


def _short(value: Any, limit: int = 80) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        text = ", ".join(_short(v, 30) for v in value) or "none"
    elif isinstance(value, dict):
        text = ", ".join(f"{k}: {_short(v, 30)}" for k, v in value.items()) or "none"
    elif value is None:
        text = "not set"
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def _label(path: str) -> str:
    head, _, rest = path.partition(".")
    section = SECTIONS.get(head, head.replace("_", " ").capitalize())
    return f"{section} {rest.replace('.', ' ').replace('_', ' ')}".strip()


def config_changes(active: dict[str, Any] | None, proposed: dict[str, Any]) -> list[dict[str, str]]:
    """Plain lines for what a proposed configuration changes compared with the active one."""
    changes: list[dict[str, str]] = []
    before = active or {}
    children = proposed.get("children")
    if children and not before.get("children"):
        github = children.get("github") or {}
        label = children.get("label") or "child project"
        changes.append({"kind": "+", "text": f"Can create {label}s in {children.get('path')} from the template "
                                             f"{children.get('template')}, as {'private' if github.get('private', True) else 'public'} "
                                             f"repositories {github.get('owner')}/{github.get('repo_name')}"})
    old, new = _flatten({k: v for k, v in before.items() if not (k == "children" and children and not before.get("children"))}), \
        _flatten({k: v for k, v in proposed.items() if not (k == "children" and children and not before.get("children"))})
    for path in sorted(set(old) | set(new)):
        if old.get(path) == new.get(path):
            continue
        if path not in old:
            changes.append({"kind": "+", "text": f"{_label(path)}: {_short(new[path])}"})
        elif path not in new:
            changes.append({"kind": "-", "text": f"{_label(path)} removed (was {_short(old[path])})"})
        else:
            changes.append({"kind": "~", "text": f"{_label(path)}: {_short(old[path])} → {_short(new[path])}"})
    # The most consequential sections first: what agents run, test, and reach.
    order = ["+", "~", "-"]
    weight = {"Can create": 0, "Toolchain": 1, "Command": 2, "Test services": 3, "Quality gate": 4, "Network": 5, "Secrets": 5}
    changes.sort(key=lambda c: (min((w for k, w in weight.items() if c["text"].startswith(k)), default=9), order.index(c["kind"])))
    return changes[:MAX_CHANGES]


# ---------------------------------------------------------------- per action


def _config(uow: UnitOfWork, approval: Row, defaults: dict[str, Any]) -> dict[str, Any]:
    subject = approval["subject"] or {}
    uow.cur.execute("SELECT * FROM project_configs WHERE id = %s", (subject.get("config_id"),))
    proposed = uow.cur.fetchone()
    uow.cur.execute("SELECT * FROM project_configs WHERE project_id = %s AND status = 'ACTIVE'", (approval["project_id"],))
    active = uow.cur.fetchone()
    first = active is None
    # A first configuration is compared with the platform defaults: what the project itself sets.
    changes = config_changes(defaults if first else active["effective_config"], proposed["effective_config"] if proposed else {})
    head = subject.get("head_commit") or ""
    return {
        "headline": "Get this project ready for tasks, with the settings found in it?" if first
                    else f"The project's settings changed ({len(changes) or 'internal values only'}). Use the new settings?",
        "highlights": [], "recommendation": None,
        "changes": changes or [{"kind": "~", "text": "Only internal values changed (the effective configuration hash)"}],
        "why": "Project configurations decide what agents may run, test, and reach, so each one needs a person."
               + (" This is the project's first configuration." if first else ""),
        "if_yes": "The new configuration applies to the project's next tasks." if not first
                  else "The project becomes ready: you can give it tasks.",
        "if_no": "The configuration stays as it was." if not first else "The project stays registered but takes no tasks.",
        "facts": [["Source", "the repository's .hermes/project.yaml" if subject.get("source") == "REPOSITORY" else "a proposal from the scan"],
                  ["Commit", head[:12] or "—"], ["Compared with", "the platform defaults (first configuration)" if first else "the active configuration"]],
        "links": [],
    }


# High-risk command rules in plain words. Whether a command could have had an effect is decided when it is recorded
# (ho_core.policy.commands.provably_blocked: only tools absent from every worker image count as blocked).
RULE_WORDS = {"CMD-H01": "force-push code", "CMD-H02": "delete a branch on GitHub", "CMD-H03": "rewrite the code history",
              "CMD-H04": "merge or delete on GitHub", "CMD-H05": "delete files outside their work folder",
              "CMD-H06": "delete database data", "CMD-H07": "control containers (Docker)", "CMD-H08": "change infrastructure",
              "CMD-H09": "use administrator permissions or change permissions", "CMD-H10": "change cloud services",
              "CMD-H11": "read passwords or keys"}


GATE_REASONS = {"without running any test": "code changed without running any test", "critical change": "it is a critical change",
                "inadequate tests": "a sensitive change has too few tests"}


def _plain_reason(reason: str) -> str:
    return next((words for key, words in GATE_REASONS.items() if key in reason), reason)


def _recommend(action: str, text: str) -> dict[str, str]:
    """`approve`, `reject`, or `look` (no clear answer: read the details)."""
    return {"action": action, "text": text}


def _checks(uow: UnitOfWork, task_id: Any) -> tuple[str | None, list[Row]]:
    """The latest verification state and the latest integration reviews of a task."""
    uow.cur.execute("SELECT state FROM verifications WHERE task_id = %s ORDER BY created_at DESC LIMIT 1", (task_id,))
    tests = (uow.cur.fetchone() or {}).get("state")
    uow.cur.execute("SELECT reviewer_provider, outcome FROM reviews WHERE task_id = %s AND subtask_id IS NULL "
                    "ORDER BY created_at DESC LIMIT 2", (task_id,))
    return tests, uow.cur.fetchall()


def _task(uow: UnitOfWork, approval: Row) -> Row | None:
    if approval["task_id"] is None:
        return None
    uow.cur.execute("SELECT * FROM tasks WHERE id = %s", (approval["task_id"],))
    return uow.cur.fetchone()


def _merge(uow: UnitOfWork, approval: Row) -> dict[str, Any]:
    subject, tid = approval["subject"] or {}, approval["task_id"]
    uow.cur.execute("SELECT * FROM git_changes WHERE task_id = %s", (tid,))
    git = uow.cur.fetchone() or {}
    uow.cur.execute("SELECT s.local_key, s.title FROM subtasks s JOIN tasks t ON t.id = s.task_id WHERE s.task_id = %s "
                    "AND s.plan_version = t.current_plan_version AND s.state IN ('ACCEPTED', 'INTEGRATED') ORDER BY s.key", (tid,))
    included = [{"kind": "+", "text": f"{r['local_key']}: {r['title']}"} for r in uow.cur.fetchall()]
    uow.cur.execute("SELECT summary FROM events WHERE task_id = %s AND type = 'INTEGRATION_COMPLETED' ORDER BY seq DESC LIMIT 1", (tid,))
    integrated = (uow.cur.fetchone() or {}).get("summary")
    tests, review_rows = _checks(uow, tid)
    reviews = ", ".join(f"{r['outcome'].lower()} by {r['reviewer_provider']}" for r in review_rows) or "—"
    branch = subject.get("target_branch") or "main"
    pr = subject.get("pr_number")
    approved = bool(review_rows) and all(r["outcome"] == "APPROVED" for r in review_rows)
    good = tests == "PASSED" and approved and subject.get("quality_gate")
    return {
        "headline": f"The work is finished and checked. Add it to {branch}?",
        "highlights": [("Tests passed" if tests == "PASSED" else f"Tests: {(tests or 'not run').lower()}"),
                       ("Reviewed and approved" if approved else "Review: " + reviews),
                       f"{len(included)} part(s) included" if included else "One change"],
        "recommendation": _recommend("approve", "Tests passed and the review approved it.") if good
        else _recommend("look", "Not every check passed: see the technical details."),
        "changes": included or [{"kind": "~", "text": integrated or "the task's integrated change"}],
        "why": f"Nothing enters {branch} without your approval, bound to these exact commits.",
        "if_yes": f"{'Pull request #' + str(pr) if pr else 'The change'} is merged into {branch}, then verified after the merge.",
        "if_no": "Nothing is merged; the task goes back for fixes.",
        "facts": [["Into", branch], ["Change", (integrated or "—").split(": ", 1)[-1]], ["Tests", (tests or "—").lower()],
                  ["Review", reviews], ["Quality gate", "passed" if subject.get("quality_gate") else "—"],
                  ["Commit", str(subject.get("head_sha") or "")[:12]]],
        "links": [{"label": f"Pull request #{pr}", "url": git["pr_url"]}] if pr and git.get("pr_url") else [],
    }


def _high_risk(uow: UnitOfWork, approval: Row) -> dict[str, Any]:
    subject, tid = approval["subject"] or {}, approval["task_id"]
    if subject.get("kind") == "human_change_conflict":
        files = subject.get("overlapping") or []
        return {
            "headline": f"You and the agents changed the same file(s) ({len(files) or 'some'}). Let the task continue on top of your changes?",
            "highlights": [], "recommendation": None,
            "changes": [{"kind": "~", "text": f"{f.get('path', f) if isinstance(f, dict) else f}"} for f in files][:MAX_CHANGES]
            or [{"kind": "~", "text": "files changed by both you and the task"}],
            "why": "Your own changes on the target branch overlap the task's changes in these files.",
            "if_yes": "The task continues on top of your changes; the overlap is resolved when it integrates.",
            "if_no": "The task stops so the overlap can be looked at.",
            "facts": [["Target commit", str(subject.get("target_sha") or "")[:12]], ["Task commit", str(subject.get("head_sha") or "")[:12]]],
            "links": [],
        }
    changes, commands, count, unproven = [], [], 0, 0
    uow.cur.execute("SELECT summary, data FROM events WHERE task_id = %s AND type = 'COMMAND_HIGH_RISK' ORDER BY seq", (tid,))
    for row in uow.cur.fetchall():
        data = row["data"] or {}
        found = re.match(r"(\d+) ", row["summary"])
        observed = int(data.get("count") or (found.group(1) if found else 1))
        count += observed
        # Recorded before the platform knew which commands could not run: none of them is taken as blocked.
        unproven += int(data["unproven"]) if "unproven" in data else observed
        who = " ".join(str(x).lower() for x in (data.get("role"), data.get("provider")) if x) or "an agent"
        for item in data.get("commands") or []:
            commands.append({"who": who, "command": item.get("command"), "rule": item.get("rule"),
                             "meaning": RULE_WORDS.get(item.get("rule"), "run a risky command")})
        if not data.get("commands"):
            changes.append({"kind": "!", "text": row["summary"]})
    uow.cur.execute("SELECT summary FROM policy_decisions WHERE task_id = %s AND decision = 'DENY'", (tid,))
    denied = [r["summary"] for r in uow.cur.fetchall()]
    changes += [{"kind": "!", "text": f"refused by policy: {d}"} for d in denied]
    uow.cur.execute("SELECT requirements FROM quality_gate_evaluations WHERE task_id = %s ORDER BY evaluated_at DESC LIMIT 1", (tid,))
    gate = uow.cur.fetchone()
    # The gate's own line for policy violations only repeats the commands above.
    failing = [r for r in (gate or {}).get("requirements") or [] if r.get("status") in ("FAIL", "APPROVAL_REQUIRED")
               and r.get("name") not in ("exception_approval", "no_policy_violations")]
    changes += [{"kind": "!", "text": f"{r['name'].replace('_', ' ')}: {r.get('detail') or r['status'].lower()}"} for r in failing]
    reasons = list(subject.get("reasons") or [])
    other = [r for r in reasons if not r.startswith("policy violations")]
    blocked = bool(count or denied) and unproven == 0  # policy denials were refused, so had no effect
    tests, review_rows = _checks(uow, tid)
    checked = tests == "PASSED" and bool(review_rows) and all(r["outcome"] == "APPROVED" for r in review_rows)
    words = sorted({c["meaning"] for c in commands})
    example = f" (for example, {words[0]})" if words else ""
    if other:
        headline = "This change needs a closer look before it is added: " + "; ".join(_plain_reason(r) for r in other) + "."
        recommendation = _recommend("look", "Read the technical details, or ask the agents to fix it.")
    elif blocked:
        headline = (f"The agents tried {count} action(s) they are not allowed to do{example}. "
                    "They were blocked and changed nothing.")
        recommendation = (_recommend("approve", "Nothing happened, the tests passed, and the review approved the change.")
                          if checked else _recommend("look", "Nothing happened, but not every check passed."))
    else:
        headline = f"The agents ran {count} risky command(s){example}. Check what they did before continuing."
        recommendation = _recommend("look", "Some of these commands may have had an effect: see the technical details.")
    return {
        "headline": headline,
        "highlights": [],
        "recommendation": recommendation,
        "display_risk": "low" if blocked and not other else None,
        "commands": commands[:MAX_CHANGES],
        "changes": changes[:MAX_CHANGES],
        "why": "The quality gate found something that normally stops a merge: " + "; ".join(subject.get("reasons") or ["see the list"]) + ".",
        "if_yes": "The gate accepts this exception for this exact commit and the task moves on to the merge decision.",
        "if_no": "The task goes back to the agents to fix it.",
        "facts": [["Commit", str(subject.get("commit") or "")[:12]]],
        "links": [],
    }


def _budget(uow: UnitOfWork, approval: Row, unlimited: bool) -> dict[str, Any]:
    add = (approval["subject"] or {}).get("add") or {}
    uow.cur.execute("SELECT limits, consumed FROM budgets WHERE task_id = %s", (approval["task_id"],))
    budget = uow.cur.fetchone() or {"limits": {}, "consumed": {}}
    task = _task(uow, approval)
    if task is not None:  # runtime is computed from the task's timestamps, not stored (budgets.runtime_minutes)
        budget = {**budget, "consumed": {**budget["consumed"], "runtime_minutes": round(budgets.runtime_minutes(task))}}
    if unlimited:
        changes = [{"kind": "~", "text": f"{COUNTERS.get(k, k)}: {budget['consumed'].get(k, 0)} used of {v} → no limit"}
                   for k, v in budget["limits"].items() if v is not None]
    else:
        changes = [{"kind": "~", "text": f"{COUNTERS.get(k, k)}: {budget['consumed'].get(k, 0)} used of "
                                         f"{budget['limits'].get(k)} → limit {(budget['limits'].get(k) or 0) + int(n)}"}
                   for k, n in add.items()]
    plain = ", ".join(f"+{n} {COUNTERS.get(k, k)}" for k, n in add.items())
    return {
        "headline": f"The task ran out of budget. Give it more ({plain}) so it can finish?" if not unlimited
                    else "Remove every budget limit from this task?",
        "highlights": [],
        "recommendation": None if not unlimited else _recommend("reject", "A fixed raise is safer than no limit."),
        "changes": changes,
        "why": "The task used up part of its budget; giving it more is your decision." if not unlimited
               else "An unlimited budget removes every limit on this task; only a person may allow it.",
        "if_yes": "The limits are raised and the task resumes where it stopped." if not unlimited
                  else "The task runs without budget limits until it finishes.",
        "if_no": "The task stays paused; you can still cancel it or raise less." if not unlimited
                 else "The task continues with its current, limited budget.",
        "facts": [], "links": [],
    }


def _waiting(uow: UnitOfWork, approval: Row) -> dict[str, Any]:
    subject = approval["subject"] or {}
    task = _task(uow, approval)
    action = approval["action"]
    if action == "ASSUMPTION":
        change = subject.get("assumption") or approval["summary"]
        why = "The request does not say this, and the agents need it to continue."
        yes, no = "The agents continue taking it as given.", "The task stops so you can clarify the request."
    elif subject.get("kind") == "plan_expansion":
        change = f"the new plan adds {subject.get('added')} subtasks; the task's limit is {subject.get('limit')}"
        why = "The plan grew beyond the expansion limit set for this task."
        yes, no = "The agents work on the bigger plan.", "The task stays blocked; you can revise the request."
    else:
        change = subject.get("text") or approval["summary"]
        why = "The orchestrator asked you directly."
        yes, no = "The orchestrator goes ahead.", "The task stops."
    text = str(change)
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    headline = (first if len(first) <= 220 else first[:217] + "…") if action == "ASSUMPTION" or subject.get("kind") != "plan_expansion" \
        else f"The plan grew to {subject.get('added')} new subtasks (limit {subject.get('limit')}). Let the agents do the bigger plan?"
    return {"headline": headline, "highlights": [], "recommendation": None,
            "changes": [{"kind": "?", "text": text[:600]}], "why": why, "if_yes": yes, "if_no": no,
            "facts": [["Task", f"{task['key']}: {task['title']}" if task else "—"], ["Level", str(subject.get("level") or "—").lower()]],
            "links": []}


def _create_child(approval: Row) -> dict[str, Any]:
    s = approval["subject"] or {}
    repo, visibility = f"{s.get('owner')}/{s.get('repo')}", "private" if s.get("private", True) else "public"
    return {
        "headline": f"Create the new module {s.get('name')} with its own {visibility} GitHub repository?",
        "highlights": [], "recommendation": None,
        "changes": [{"kind": "+", "text": f"Folder {s.get('children_path')}/{s.get('name')} with the module skeleton and its rules"},
                    {"kind": "+", "text": f"{visibility.capitalize()} GitHub repository {repo}"},
                    {"kind": "+", "text": f"First commit: {str(s.get('commit_message') or '').replace('{name}', s.get('name', ''))}"},
                    {"kind": "+", "text": f"A new project {s.get('name')} under its parent (you approve its configuration next)"}],
        "why": "Creating repositories and folders is a person's decision.",
        "if_yes": "The module is created from the template exactly as pinned here.",
        "if_no": "Nothing is created; the name is free again.",
        "facts": [["Title", (s.get("params") or {}).get("title", "—")], ["Template", f"{s.get('template_path')} at {str(s.get('parent_sha'))[:12]}"],
                  ["Repository", repo]],
        "links": [],
    }


def _plan(approval: Row) -> dict[str, Any]:
    s = approval["subject"] or {}
    items = s.get("subtasks") or []
    changes = []
    for item in items:
        deps = ", ".join(item.get("depends_on") or [])
        files = ", ".join(((item.get("estimated_scope") or {}).get("files") or [])[:4])
        changes.append({"kind": "+", "text": f"{item['local_key']}: {item['title']} — {str(item.get('kind') or '').lower().replace('_', ' ')}, "
                                             f"risk {str(item.get('risk') or '').lower()}"
                                             + (f", by {item['preferred_provider']}" if item.get("preferred_provider") else "")
                                             + (f", after {deps}" if deps else "") + (f" · {files}" if files else "")})
    titles = [str(item.get("title") or item.get("local_key")) for item in items]
    return {
        "headline": f"The agents plan to do this in {len(items)} step(s). Start?",
        "highlights": titles[:3] + ([f"and {len(titles) - 3} more"] if len(titles) > 3 else []),
        "recommendation": None,
        "changes": changes,
        "why": "This task (or project) asks for your review of each plan before any subtask starts.",
        "if_yes": "The agents start on the plan's ready subtasks.",
        "if_no": "Nothing starts; the orchestrator writes a new plan (add a note to say what to change).",
        "facts": [["Plan", f"version {s.get('plan_version')}"], ["Requirements", f"version {s.get('requirements_version') or '—'}"],
                  ["Subtasks", str(len(items))]],
        "links": [],
    }


def details(uow: UnitOfWork, approval: Row, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    action, subject = approval["action"], approval.get("subject") or {}
    try:
        if action in ("PROJECT_READY", "PROJECT_CONFIG_CHANGE") and subject.get("config_id"):
            out = _config(uow, approval, defaults or {})
        elif action == "MERGE":
            out = _merge(uow, approval)
        elif action == "HIGH_RISK_OPERATION":
            out = _high_risk(uow, approval)
        elif action in ("BUDGET_INCREASE", "BUDGET_UNLIMITED"):
            out = _budget(uow, approval, action == "BUDGET_UNLIMITED")
        elif action in ("ASSUMPTION", "SCOPE_EXPANSION"):
            out = _waiting(uow, approval)
        elif action == "CREATE_CHILD":
            out = _create_child(approval)
        elif action == "PLAN":
            out = _plan(approval)
        elif action == "UPDATE":
            out = {"headline": f"Update the platform from {subject.get('from_version')} to {subject.get('to_version')}?",
                   "changes": [{"kind": "~", "text": f"platform {subject.get('from_version')} → {subject.get('to_version')}"}],
                   "why": "Platform updates need a person.", "if_yes": "The platform is updated (with a rollback point).",
                   "if_no": "The platform stays on its current version.", "facts": [], "links": []}
        else:
            out = {"changes": [{"kind": "~", "text": f"{k.replace('_', ' ')}: {_short(v)}"} for k, v in subject.items()][:MAX_CHANGES],
                   "why": approval["summary"], "if_yes": "", "if_no": "", "facts": [], "links": []}
    except Exception:  # noqa: BLE001 - a description must never hide the decision itself
        log.exception("details of approval %s failed", approval.get("id"))
        out = {"changes": [], "why": approval["summary"], "if_yes": "", "if_no": "", "facts": [], "links": []}
    risk = str(approval.get("risk") or "MEDIUM").lower()
    return {"highlights": [], "recommendation": None, **out, "headline": out.get("headline") or approval["summary"],
            "risk": out.pop("display_risk", None) or risk, "requested_by": approval.get("requested_by"),
            "requested_at": approval.get("requested_at")}
