"""Chat text for notifications: an emoji per kind, plain words, and short replies the chat plugin turns into commands.

Approvals say what is being decided and what each option does, and offer replies such as "si 12" / "no 12" (the
approval's short number). The plugin maps those replies to `/orch approve|reject`; the control plane still checks
who answers. English (`en`) and Spanish (`es`), chosen with HO_NOTIFY_LANGUAGE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

EMOJI = {
    "APPROVAL_REQUIRED": "🟡", "READY_FOR_MERGE": "🚀", "TASK_COMPLETED": "✅", "BLOCKED": "⛔", "TEST_FAILED": "❌",
    "TASK_FAILED": "❌", "AUTH_REQUIRED": "🔑", "PAUSED_BUDGET": "💰", "RECOVERY_FAILED": "⚠️", "PLATFORM_DEGRADED": "⚠️",
    "NOTIFICATION_TEST": "🧪", "DIGEST": "📋",
}

# Reply words the chat plugin understands (hermes/plugins/orchestration: SHORT_REPLIES).
_WORDS = {"en": {"yes": "yes", "no": "no", "see": "see", "retry": "retry", "cancel": "cancel"},
          "es": {"yes": "si", "no": "no", "see": "ver", "retry": "reintentar", "cancel": "cancelar"}}

_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "decide": "{emoji} *I need your decision*",
        "reply": "*Reply with one option:*",
        "see": "see the task's details",
        "expires": "⏳ Expires in {left}",
        "ready": "{emoji} *{task} is ready to merge*\n“{title}” passed tests, review, and the quality gate. The merge approval comes next.",
        "completed": "{emoji} *{task} completed*\n“{title}” is merged and verified.",
        "blocked": "{emoji} *{task} is blocked*\nWhy: {summary}",
        "blocked_options": "🔎 *{see} {task}*: see what happened\n🔁 *{retry} {task}*: try again\n🗑️ *{cancel} {task}*: cancel it",
        "test_failed": "{emoji} *{task}: tests failed*\n{summary}\n🔎 *see {task}*: see what failed",
        "task_failed": "{emoji} *{task} failed*\n{summary}",
        "auth": "{emoji} *A login has expired*\n{summary}\nOn your Mac: `make auth-claude` or `make auth-codex`.",
        "budget": "{emoji} *{task} ran out of budget*\n{summary}\nTo raise it: `/orch budget {task} agent_launches=10`",
        "platform": "{emoji} *Platform problem*\n{summary}",
        "test": "{emoji} *Test*\nhermes-orchestrator notifications reach this chat.",
        "digest": "{emoji} *Progress* ({count})",
        "more": "… and {count} more (/orch tasks)",
        "days": "{n} days", "hours": "{n} hours", "minutes": "{n} minutes",
    },
    "es": {
        "decide": "{emoji} *Necesito tu decisión*",
        "reply": "*Responde con una opción:*",
        "see": "ver el detalle de la tarea",
        "expires": "⏳ Caduca en {left}",
        "ready": "{emoji} *{task} lista para unir*\n«{title}» pasó los tests, la revisión y el control de calidad. Ahora te pido aprobar el merge.",
        "completed": "{emoji} *{task} completada*\n«{title}» ya está unida y verificada.",
        "blocked": "{emoji} *{task} está bloqueada*\nMotivo: {summary}",
        "blocked_options": "🔎 *{see} {task}*: ver qué pasó\n🔁 *{retry} {task}*: intentarlo de nuevo\n🗑️ *{cancel} {task}*: cancelarla",
        "test_failed": "{emoji} *{task}: fallaron los tests*\n{summary}\n🔎 *ver {task}*: ver qué falló",
        "task_failed": "{emoji} *{task} falló*\n{summary}",
        "auth": "{emoji} *Hay que volver a iniciar sesión*\n{summary}\nEn tu Mac: `make auth-claude` o `make auth-codex`.",
        "budget": "{emoji} *{task} se quedó sin presupuesto*\n{summary}\nPara ampliarlo: `/orch budget {task} agent_launches=10`",
        "platform": "{emoji} *Problema en la plataforma*\n{summary}",
        "test": "{emoji} *Prueba*\nLos avisos de hermes-orchestrator llegan a este chat.",
        "digest": "{emoji} *Avances* ({count})",
        "more": "… y {count} más (/orch tasks)",
        "days": "{n} días", "hours": "{n} horas", "minutes": "{n} minutos",
    },
}

# What each kind of approval asks, and what yes and no do. {fields} come from the approval and its task.
_APPROVAL: dict[str, dict[str, tuple[str, str, str]]] = {
    "en": {
        "MERGE": ("The work passed tests, review, and the quality gate. Merge it into *{branch}*?",
                  "yes, merge it into {branch}", "no, send it back for fixes"),
        "ASSUMPTION": ("To continue, Claude will take this as given:\n_{assumption}_\nIs that right?",
                       "that's right, continue", "no, stop the task"),
        "SCOPE_EXPANSION": ("The plan grew: it adds {added} subtasks and the limit is {limit}. Go on with the bigger plan?",
                            "yes, go on", "no, stop the task (it stays blocked)"),
        "BUDGET_INCREASE": ("The task needs more budget: {budget}. Raise it?", "yes, raise it", "no, leave it paused"),
        "BUDGET_UNLIMITED": ("An *unlimited* budget was requested for this task. Allow it?", "yes, no limit",
                             "no, use the normal budget"),
        "CONFLICT": ("Your changes on the main branch clash with the task's changes. Continue anyway?", "yes, continue",
                     "no, stop so it can be reviewed"),
        "GATE": ("The quality gate found something that normally stops a merge:\n{reasons}\nContinue anyway?",
                 "yes, continue", "no, have it fixed"),
        "PROJECT_READY": ("There is a new configuration for project *{project}*. Approve it so the project can take tasks?",
                          "yes, approve it", "no, reject it"),
        "PROJECT_CONFIG_CHANGE": ("Project *{project}* changed its configuration. Apply the new one?", "yes, apply it",
                                  "no, keep the current one"),
        "ENVIRONMENT_ACCESS": ("{summary}. Allow it?", "yes, allow it", "no"),
        "UPDATE": ("Update the platform from *{from_version}* to *{to_version}*?", "yes, update", "no, not now"),
        "CREATE_CHILD": ("Create *{child}* ({child_title}) in project *{project}*: a new folder {child_path} and the "
                         "{visibility} GitHub repository *{child_repo}*, from the template at {parent_sha}?",
                         "yes, create it", "no, don't create it"),
        "REQUEST": ("Claude asks:\n_{text}_", "yes, go ahead", "no, stop the task"),
        "OTHER": ("{summary}", "yes", "no"),
    },
    "es": {
        "MERGE": ("El trabajo pasó los tests, la revisión y el control de calidad. ¿Lo uno a *{branch}*?",
                  "sí, unirlo a {branch}", "no, que vuelva a corregirse"),
        "ASSUMPTION": ("Para seguir, Claude va a dar esto por hecho:\n_{assumption}_\n¿Es correcto?",
                       "es correcto, sigue", "no es así, para la tarea"),
        "SCOPE_EXPANSION": ("El plan creció: añade {added} subtareas y el límite es {limit}. ¿Sigo con el plan más grande?",
                            "sí, seguir", "no, parar la tarea (queda bloqueada)"),
        "BUDGET_INCREASE": ("La tarea necesita más presupuesto: {budget}. ¿Lo amplío?", "sí, ampliarlo", "no, dejarla pausada"),
        "BUDGET_UNLIMITED": ("Se pidió presupuesto *sin límite* para esta tarea. ¿Lo autorizo?", "sí, sin límite",
                             "no, usar el presupuesto normal"),
        "CONFLICT": ("Hay cambios tuyos en la rama principal que chocan con los de la tarea. ¿Continúo de todas formas?",
                     "sí, continuar", "no, parar para revisarlo"),
        "GATE": ("El control de calidad encontró algo que normalmente frena el merge:\n{reasons}\n¿Sigo de todas formas?",
                 "sí, seguir", "no, que se corrija"),
        "PROJECT_READY": ("Hay una configuración nueva para el proyecto *{project}*. ¿La apruebo para que pueda recibir tareas?",
                          "sí, aprobarla", "no, rechazarla"),
        "PROJECT_CONFIG_CHANGE": ("El proyecto *{project}* cambió su configuración. ¿Aplico la nueva?", "sí, aplicarla",
                                  "no, mantener la actual"),
        "ENVIRONMENT_ACCESS": ("{summary}. ¿Lo permito?", "sí, permitirlo", "no"),
        "UPDATE": ("¿Actualizo la plataforma de *{from_version}* a *{to_version}*?", "sí, actualizar", "ahora no"),
        "CREATE_CHILD": ("¿Creo *{child}* ({child_title}) en el proyecto *{project}*: una carpeta nueva {child_path} y el "
                         "repositorio {visibility_es} de GitHub *{child_repo}*, desde la plantilla en {parent_sha}?",
                         "sí, créalo", "no, no lo crees"),
        "REQUEST": ("Claude pregunta:\n_{text}_", "sí, adelante", "no, para la tarea"),
        "OTHER": ("{summary}", "sí", "no"),
    },
}

_COUNTERS = {
    "en": {"agent_launches": "agent launches", "runtime_minutes": "minutes", "retries": "retries", "subtasks": "subtasks",
           "review_cycles": "review cycles", "provider_usage_units": "usage units"},
    "es": {"agent_launches": "lanzamientos de agentes", "runtime_minutes": "minutos", "retries": "reintentos",
           "subtasks": "subtareas", "review_cycles": "ciclos de revisión", "provider_usage_units": "unidades de uso"},
}
# Quality Gate exception reasons (ho_core.verification), in plain words.
_REASONS = {
    "es": {"without running any test": "se cambió código sin ejecutar ningún test",
           "critical change": "es un cambio crítico", "inadequate tests": "cambio sensible con tests insuficientes",
           "policy violations": "los agentes ejecutaron comandos que la política marca como de riesgo"},
    "en": {"without running any test": "code changed without running any test", "critical change": "a critical change",
           "inadequate tests": "a sensitive change with inadequate tests",
           "policy violations": "the agents ran commands the policy marks as risky"},
}
# Routine events in the digest.
_LABELS = {
    "en": {"TASK_CREATED": "task created", "TASK_CANCELLED": "cancelled", "PROJECT_READY": "project ready",
           "PLAN_VERSIONED": "new plan", "SUBTASK_ACCEPTED": "subtask accepted", "INTEGRATION_COMPLETED": "work integrated",
           "TEST_PASSED": "tests passed", "REVIEW_PASSED": "review passed", "REVIEW_FAILED": "review asked for changes",
           "PR_CREATED": "pull request created", "MERGE_COMPLETED": "merged", "BUDGET_THRESHOLD": "budget almost used",
           "FAILOVER_COMPLETED": "the orchestrator switched provider", "ORCHESTRATOR_FAILBACK": "the orchestrator is back on Claude",
           "PLATFORM_RECOVERED": "platform recovered", "REQUIREMENTS_REVISED": "requirements revised"},
    "es": {"TASK_CREATED": "tarea creada", "TASK_CANCELLED": "cancelada", "PROJECT_READY": "proyecto listo",
           "PLAN_VERSIONED": "plan nuevo", "SUBTASK_ACCEPTED": "subtarea aceptada", "INTEGRATION_COMPLETED": "trabajo integrado",
           "TEST_PASSED": "tests en verde", "REVIEW_PASSED": "revisión aprobada", "REVIEW_FAILED": "la revisión pidió cambios",
           "PR_CREATED": "PR creado", "MERGE_COMPLETED": "merge hecho", "BUDGET_THRESHOLD": "presupuesto casi agotado",
           "FAILOVER_COMPLETED": "el orquestador cambió de proveedor", "ORCHESTRATOR_FAILBACK": "el orquestador volvió a Claude",
           "PLATFORM_RECOVERED": "plataforma recuperada", "REQUIREMENTS_REVISED": "requisitos revisados"},
}


@dataclass(frozen=True)
class Context:
    """What a notification refers to, loaded by the deliverer."""

    task: str | None = None  # T-15
    title: str | None = None
    project: str | None = None
    approval: dict[str, Any] | None = None  # the approval row for APPROVAL_REQUIRED


def language(value: Any) -> str:
    return value if value in _TEXT else "en"


def render(payload: dict[str, Any], ctx: Context, lang: str = "en", now: datetime | None = None) -> str:
    lang = language(lang)
    event = payload.get("type", "")
    text, emoji = _TEXT[lang], EMOJI.get(event, "ℹ️")
    summary = _strip_task(str(payload.get("summary") or event), ctx.task)
    task, title = ctx.task or "", ctx.title or ctx.task or summary
    if event == "APPROVAL_REQUIRED" and ctx.approval is not None:
        return _approval(ctx, lang, now or datetime.now(timezone.utc))
    simple = {"READY_FOR_MERGE": "ready", "TASK_COMPLETED": "completed", "TEST_FAILED": "test_failed",
              "TASK_FAILED": "task_failed", "AUTH_REQUIRED": "auth", "PAUSED_BUDGET": "budget",
              "RECOVERY_FAILED": "platform", "PLATFORM_DEGRADED": "platform", "NOTIFICATION_TEST": "test"}
    if event == "BLOCKED":
        words = _WORDS[lang]
        return (text["blocked"].format(emoji=emoji, task=task, summary=summary) + "\n" + text["reply"] + "\n"
                + text["blocked_options"].format(task=task, **words))
    if event in simple:
        return text[simple[event]].format(emoji=emoji, task=task, title=title, summary=summary)
    head = f"{emoji} *{task}* " if task else f"{emoji} "
    return head + summary


def render_digest(items: list[tuple[dict[str, Any], Context]], lang: str = "en", limit: int = 20) -> str:
    lang = language(lang)
    text, labels = _TEXT[lang], _LABELS[lang]
    lines = [text["digest"].format(emoji=EMOJI["DIGEST"], count=len(items))]
    for payload, ctx in items[:limit]:
        label = labels.get(payload.get("type", ""), _strip_task(str(payload.get("summary") or ""), ctx.task))
        who = ctx.task or ctx.project  # project-level events (a project ready) name the project
        lines.append(f"• {who}: {label}" if who else f"• {label}")
    if len(items) > limit:
        lines.append(text["more"].format(count=len(items) - limit))
    return "\n".join(lines)


def plain_approval(approval: dict[str, Any], *, task: str | None = None, project: str | None = None,
                   lang: str = "en") -> dict[str, Any]:
    """What an approval asks and what yes and no do, in plain words without chat markup (the Dashboard)."""
    kind, question, yes, no, fields = _approval_parts(approval, task, project, lang)

    def plain(template: str) -> str:
        return template.replace("*", "").replace("_{", "{").replace("}_", "}").format(**fields)

    return {"kind": kind, "question": plain(question), "yes": plain(yes), "no": plain(no),
            "reasons": _reasons(fields["summary"], lang) if kind == "GATE" else []}


def _approval_parts(approval: dict[str, Any], task: str | None, project: str | None,
                    lang: str) -> tuple[str, str, str, str, dict[str, Any]]:
    subject = approval.get("subject") or {}
    action = str(approval.get("action") or "")
    kind = action
    if action == "HIGH_RISK_OPERATION":
        kind = "CONFLICT" if subject.get("kind") == "human_change_conflict" else "GATE"
    elif action == "SCOPE_EXPANSION" and subject.get("kind") != "plan_expansion":
        kind = "REQUEST"  # the orchestrator's own question (REQUEST_APPROVAL), shown as asked
    question, yes, no = _APPROVAL[lang].get(kind, _APPROVAL[lang]["OTHER"])
    summary = _strip_task(str(approval.get("summary") or action), task)
    fields = {
        "branch": subject.get("target_branch") or "main",
        "assumption": str(subject.get("assumption") or summary)[:500],
        "added": subject.get("added", "?"), "limit": subject.get("limit", "?"),
        "budget": ", ".join(f"+{v} {_COUNTERS[lang].get(k, k)}" for k, v in (subject.get("add") or {}).items()) or summary,
        "reasons": "\n".join(f"• {r}" for r in _reasons(summary, lang)),
        "project": project or "", "summary": summary, "text": str(subject.get("text") or summary)[:600],
        "from_version": subject.get("from_version", "?"), "to_version": subject.get("to_version", "?"),
        "child": subject.get("name", "?"), "child_title": (subject.get("params") or {}).get("title", ""),
        "child_repo": f"{subject.get('owner', '?')}/{subject.get('repo', '?')}",
        "child_path": f"{subject.get('children_path', '?')}/{subject.get('name', '?')}",
        "visibility": "private" if subject.get("private", True) else "public",
        "visibility_es": "privado" if subject.get("private", True) else "público",
        "parent_sha": str(subject.get("parent_sha") or "?")[:12],
    }
    return kind, question, yes, no, fields


def _approval(ctx: Context, lang: str, now: datetime) -> str:
    approval = ctx.approval or {}
    _, question, yes, no, fields = _approval_parts(approval, ctx.task, ctx.project, lang)
    text, words, number = _TEXT[lang], _WORDS[lang], approval.get("number")
    ref = number if number is not None else approval.get("id")
    who = f" · {ctx.task}" + (f" «{ctx.title}»" if lang == "es" and ctx.title else f" “{ctx.title}”" if ctx.title else "") if ctx.task else ""
    lines = [text["decide"].format(emoji=EMOJI["APPROVAL_REQUIRED"]) + who, question.format(**fields), "", text["reply"],
             f"✅ *{words['yes']} {ref}*: {yes.format(**fields)}", f"❌ *{words['no']} {ref}*: {no.format(**fields)}"]
    if ctx.task:
        lines.append(f"🔎 *{words['see']} {ctx.task}*: {text['see']}")
    expires = approval.get("expires_at")
    if isinstance(expires, datetime):
        lines.append(text["expires"].format(left=_left(expires - now, text)))
    return "\n".join(lines)


def _reasons(summary: str, lang: str) -> list[str]:
    raw = summary.split("needed:", 1)[1] if "needed:" in summary else summary
    out = []
    for reason in (r.strip() for r in raw.split(";") if r.strip()):
        plain = next((v for k, v in _REASONS[lang].items() if k in reason), reason)
        out.append(plain)
    return out or [summary]


def _left(delta: Any, text: dict[str, str]) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds >= 2 * 86400:
        return text["days"].format(n=seconds // 86400)
    if seconds >= 2 * 3600:
        return text["hours"].format(n=seconds // 3600)
    return text["minutes"].format(n=max(1, seconds // 60))


def _strip_task(summary: str, task: str | None) -> str:
    """Event summaries often start with the task key; the message already names the task."""
    if task and summary.startswith(f"{task}: "):
        return summary[len(task) + 2:]
    return summary
