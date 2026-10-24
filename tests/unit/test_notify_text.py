"""Chat notification wording: emojis, plain explanations, and the short replies they offer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from control_plane.notify_text import Context, render, render_digest

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)


def approval(action, subject, summary="s", requested_by="orchestrator", number=12, days=3):
    return {"id": "01a0-uuid", "number": number, "action": action, "subject": subject, "summary": summary,
            "requested_by": requested_by, "expires_at": NOW + timedelta(days=days)}


def ask(action, subject, lang="es", **kw):
    ctx = Context(task="T-15", title="Search job online", project="job-finder", approval=approval(action, subject, **kw))
    return render({"type": "APPROVAL_REQUIRED", "summary": "x"}, ctx, lang, now=NOW)


def test_an_assumption_is_explained_with_options_to_pick():
    text = ask("ASSUMPTION", {"assumption": "buscar ofertas = ordenar un catálogo local"})
    assert text.startswith("🟡 *Necesito tu decisión* · T-15 «Search job online»")
    assert "_buscar ofertas = ordenar un catálogo local_" in text and "¿Es correcto?" in text
    assert "✅ *si 12*: es correcto, sigue" in text and "❌ *no 12*: no es así, para la tarea" in text
    assert "🔎 *ver T-15*: ver el detalle de la tarea" in text and "⏳ Caduca en 3 días" in text
    english = ask("ASSUMPTION", {"assumption": "x"}, lang="en")
    assert "✅ *yes 12*: that's right, continue" in english and "🔎 *see T-15*" in english


def test_each_kind_of_approval_says_what_is_decided():
    assert "¿Lo uno a *main*?" in ask("MERGE", {"target_branch": "main"}) and "sí, unirlo a main" in ask("MERGE", {"target_branch": "main"})
    assert "+10 lanzamientos de agentes, +120 minutos" in ask("BUDGET_INCREASE", {"add": {"agent_launches": 10, "runtime_minutes": 120}})
    assert "añade 4 subtareas y el límite es 2" in ask("SCOPE_EXPANSION", {"kind": "plan_expansion", "added": 4, "limit": 2})
    gate = ask("HIGH_RISK_OPERATION", {"verification": "v"}, requested_by="quality-gate",
               summary="T-15: Quality Gate exception needed: code change verified without running any test (section 56); "
                       "policy violations were observed (section 58)")
    assert "• se cambió código sin ejecutar ningún test" in gate and "• los agentes ejecutaron comandos" in gate
    conflict = ask("HIGH_RISK_OPERATION", {"kind": "human_change_conflict"})
    assert "chocan con los de la tarea" in conflict
    assert "proyecto *job-finder*" in ask("PROJECT_READY", {"config_id": "c"})
    assert "de *1.0* a *1.1*" in ask("UPDATE", {"from_version": "1.0", "to_version": "1.1"})
    assert "⏳ Caduca en 5 horas" in ask("MERGE", {}, days=5 / 24)


def test_other_events_have_an_emoji_and_plain_words():
    ctx = Context(task="T-15", title="Search job online")
    assert render({"type": "TASK_COMPLETED", "summary": "T-15 completed"}, ctx, "es").startswith("✅ *T-15 completada*")
    assert "🚀 *T-15 lista para unir*" in render({"type": "READY_FOR_MERGE", "summary": "x"}, ctx, "es")
    blocked = render({"type": "BLOCKED", "summary": "T-15: integration conflict"}, ctx, "es")
    assert blocked.startswith("⛔ *T-15 está bloqueada*\nMotivo: integration conflict")
    assert "🔁 *reintentar T-15*" in blocked and "🗑️ *cancelar T-15*" in blocked
    assert render({"type": "PAUSED_BUDGET", "summary": "x"}, ctx, "es").startswith("💰")
    assert render({"type": "NOTIFICATION_TEST", "summary": "x"}, Context(), "es").startswith("🧪 *Prueba*")


def test_the_digest_lists_progress_in_plain_words():
    items = [({"type": "PLAN_VERSIONED", "summary": "plan v2"}, Context(task="T-15")),
             ({"type": "TEST_PASSED", "summary": "ok"}, Context(task="T-16"))]
    assert render_digest(items, "es") == "📋 *Avances* (2)\n• T-15: plan nuevo\n• T-16: tests en verde"


def test_the_orchestrators_own_question_is_shown_as_asked():
    text = ask("SCOPE_EXPANSION", {"kind": "orchestrator_request", "text": "¿Puedo borrar el módulo antiguo?"})
    assert "Claude pregunta:\n_¿Puedo borrar el módulo antiguo?_" in text
    assert "subtareas" not in text and "✅ *si 12*: sí, adelante" in text


def test_failed_tests_do_not_promise_a_repair():
    text = render({"type": "TEST_FAILED", "summary": "post-merge verification failed"}, Context(task="T-15"), "es")
    assert "solo" not in text and "🔎 *ver T-15*" in text


def test_options_say_what_really_happens_and_digests_name_projects():
    assert "❌ *no 12*: no, que vuelva a corregirse" in ask("MERGE", {"target_branch": "main"})
    assert "queda bloqueada" in ask("SCOPE_EXPANSION", {"kind": "plan_expansion", "added": 3, "limit": 1})
    items = [({"type": "PROJECT_READY", "summary": "demo is ready"}, Context(project="demo"))]
    assert render_digest(items, "es") == "📋 *Avances* (1)\n• demo: proyecto listo"


def test_a_decision_lists_its_first_changes():
    ctx = Context(project="odoo-base", approval=approval("PROJECT_READY", {"config_id": "x"}),
                  details={"changes": [{"kind": "+", "text": "Can create modules"}, {"kind": "~", "text": "Toolchain profiles: python → odoo"},
                                       {"kind": "~", "text": "Command test: a → b"}, {"kind": "-", "text": "Network removed"}]})
    text = render({"type": "APPROVAL_REQUIRED", "summary": "x"}, ctx, "en", now=NOW)
    assert "*What changes:*\n➕ Can create modules\n✏️ Toolchain profiles: python → odoo\n✏️ Command test: a → b\n… and 1 more in the Dashboard" in text
    assert "Qué cambia" in render({"type": "APPROVAL_REQUIRED", "summary": "x"}, ctx, "es", now=NOW)


def test_a_ui_design_decision_offers_to_see_the_design_again():
    text = ask("UI_DESIGN", {"kind": "ui_design", "version": 1})
    assert "🎨 *diseño T-15*: ver otra vez el diseño propuesto" in text
    assert "🎨 *design T-15*: see the proposed design again" in ask("UI_DESIGN", {"kind": "ui_design"}, lang="en")
    assert "diseño T-15" not in ask("MERGE", {"target_branch": "main"})
