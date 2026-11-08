"""Headless pen.dev design packages and an enforced, version-bound visual decision."""
from __future__ import annotations

import re
import unicodedata

from ho_core import attachments
from ho_core.enums import ApprovalAction, TaskState as S
from ho_core.hashing import hash_value, sha256_hex
from ho_core.ids import uuid7
from ho_core.statemachine import ACTIVE_STATES, Trigger

from .db import jsonb
from .errors import ApiError, Conflict, NotFound
from .events import record_event

IMAGES = ('desktop.png', 'mobile.png')
# The implementation package: clean images (the specification), notes and source. Agents receive only these.
PACKAGE = (*IMAGES, 'design.md', 'design.pen')
# The person's view of what changed: the proposal with numbered markers. Never part of the implementation package, so a
# marker is never mistaken for part of the design; covered by the manifest hash the approval binds.
CHANGES = 'changes.png'
COLLECTED = (CHANGES, *PACKAGE)


def shown(design) -> tuple[str, ...]:
    """The images a person sees before deciding, changes first (designs made before changes.png have none)."""
    return ((CHANGES,) if CHANGES in ((design or {}).get('manifest') or {}) else ()) + IMAGES
# design.md sections a person reads before deciding, in order of preference (older designs have only before/after).
SUMMARY_HEADINGS = ('summary for the reviewer', 'before / after', 'before/after')
SUMMARY_LIMIT = 1500
LANGUAGE_NAMES = {'en': 'English', 'es': 'Spanish'}
_HEADING = re.compile(r'^#{2,3}\s+(.+?)\s*#*\s*$')
_AFTER = re.compile(r'^\s*[*_]*after[*_]*\s*:?[*_]*\s*:?\s*', re.I)
_UI = re.compile(r'\b(front[ -]?end|ui|ux|portfolio|portafolio|landing|dashboard|interfaz|pantalla|maquetacion|'
                 r'pagina web|sitio web|website|web page|user interface|visual design|diseno visual|css|html)\b|\.pen\b', re.I)
_UI_PATH = re.compile(r'(^|/)(ui|frontend|templates|views|static|components|pages)(/|$)|'
                      r'\.(tsx|jsx|vue|svelte|html|css|scss|sass|less|pen)$', re.I)
_NO_UI = re.compile(r'\b(?:esta tarea no tiene cambios de interfaz|no hace falta diseno de ui|'
                    r'this task has no (?:ui|interface) changes|no ui (?:changes|design)(?: is required)?)'
                    r'(?=\s*(?:[.;:\n]|$))', re.I)


def detects_ui(text: str, files: list[str] | tuple[str, ...] = ()) -> bool:
    plain = ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))
    return bool(_UI.search(plain) or any(_UI_PATH.search(p) for p in files))


def backend_scope_declared(text: str, files: list[str] | tuple[str, ...]) -> bool:
    """An explicit human no-UI declaration plus a concrete backend scope, never an agent's assertion alone."""
    plain = ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))
    if not files or any(not p.strip() or any(c in p for c in '*?[') or _UI_PATH.search(p) for p in files):
        return False
    # A conflicting positive UI request, or an unspecified surface, keeps the visual gate in place.
    return bool(_NO_UI.search(plain)) and not detects_ui(_NO_UI.sub('', plain))


def review_summary(markdown: str, limit: int = SUMMARY_LIMIT) -> str:
    """The changes a person reviews with the images: the reviewer summary, else the before/after section."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading.group(1).strip().lower()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    heading = next((h for h in SUMMARY_HEADINGS if '\n'.join(sections.get(h, [])).strip()), None)
    lines = sections[heading] if heading else []
    if heading and heading != SUMMARY_HEADINGS[0]:
        # A before/after section: what the person will see is the "after" part, when the designer marked one.
        after = next((i for i, line in enumerate(lines) if _AFTER.match(line)), None)
        if after is not None:
            lines = [_AFTER.sub('', lines[after])] + lines[after + 1:]
    text = '\n'.join(lines).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(' ', 1)[0].rstrip() + '…'
    return text


DESIGN_INSTRUCTIONS = """Create the proposed UI in pen.dev BEFORE implementing any frontend code.
Use the installed `pen interactive` headless CLI, not the VS Code/desktop MCP. No GUI is needed.
The provider running you already supplies the intelligence: use interactive tool calls, not `pen --prompt`
(which would start a second, unmetered AI agent). Run `pen interactive --help` for its actual tool schema.
Load read_skill(), read_skill({path:"pen-schema.md"}), and read_skill({path:"execute.md"}) in the shell.
Read the repository's conventions and existing UI. Existing .pen files are encrypted: open them only with
pen.dev's --in option; never read/grep/edit their contents directly. Never implement application code here.
If the request names an existing .pen file that is absent from the tracked project view and task attachments,
return BLOCKED and ask for that file as an attachment or a tracked project file; never silently replace its design.
Use `ho-pen` instead of `pen`: it loads the granted PEN_CLI_KEY from the in-memory secret file when needed.
Create or update the design with execute() tool calls; save() before exit(). Respect the project's visual style.
Draw with the components of the project's own UI framework (for example Odoo's standard form, list and kanban
views, buttons and smart buttons), so the implementation can match the images without custom widgets.
Make every change easy to find (the person must see at a glance what changed):
- Name this version's frames `vN · Before` (the current interface, when one exists), `vN · After` (desktop) and
  `vN · After mobile`, plus `vN · Changes`, side by side. Never overwrite an earlier version's frames: add the new
  version to their right.
- `vN · Changes` is a copy of `vN · After` with, around every changed or new element, a 3 px #E5007E outline and a
  numbered #E5007E circle (white number), and a legend beside it: one plain line per number. Mark an element removed
  from the interface with a dashed #E5007E box labelled "Removed" on its place. #E5007E is reserved for these
  markers: never use it in the design itself.
- In a revision after feedback, mark only what changed since the previous proposal, not since the first one.
- The After frames stay clean: no markers, no legend (they are what gets implemented).
Write ONLY these deliverables under /output/design/:
- design.pen: the saved pen.dev document, containing the frames above.
- desktop.png and mobile.png: actual PNG exports of the clean After frames, made by pen.dev.
- changes.png: the actual PNG export of the Changes frame, made by pen.dev.
- design.md: human-readable changes, frame names, breakpoints, typography, colors, spacing, interactions,
  assets and implementation guidance; explain before/after differences when an interface exists.
  Start design.md with a `## Summary for the reviewer` section: 3 to 8 short numbered items (`1.`, `2.`), in plain
  words, on what the person will see change on each screen; each number is the circle with that number in
  changes.png. It is sent with the images for the approval, so name no file, model, ID or code; keep it under
  1,200 characters.
Export with execute({input:'Export([desktopId], "png", "/tmp/pen-desktop"); Export([mobileId], "png", "/tmp/pen-mobile"); Export([changesId], "png", "/tmp/pen-changes")'}), using the real frame IDs.
IMPORTANT: Export writes a DIRECTORY containing <frame-id>.png, not a PNG at the outputPath itself.
Copy /tmp/pen-desktop/<desktopId>.png to /output/design/desktop.png,
/tmp/pen-mobile/<mobileId>.png to /output/design/mobile.png and
/tmp/pen-changes/<changesId>.png to /output/design/changes.png with the shell, then inspect the actual files.
Confirm all five deliverables are regular files (test -f) and nonempty: pen export can fail while exiting zero. Missing authentication or
rendering is BLOCKED, never a reason to invent screenshots or replace the design with application code.
The project is mounted read-only. Return the normal agent-result contract. No commits are expected.
"""


class UIDesigns:
    def __init__(self, ctx, tasks, approvals, executions):
        self.ctx, self.tasks, self.approvals, self.executions = ctx, tasks, approvals, executions
        self.recover_failure = None
        approvals.register_handler(ApprovalAction.UI_DESIGN, self._decision)
        approvals.register_renewer(ApprovalAction.UI_DESIGN, self._renew)
        executions.on_finished.append(self.finished)
        executions.ui_designs = self

    def current(self, uow, task):
        uow.cur.execute('SELECT * FROM ui_designs WHERE task_id = %s ORDER BY version DESC LIMIT 1', (task['id'],))
        return uow.cur.fetchone()

    @staticmethod
    def matches(design, task):
        # The design follows the requirements, not the plan: a re-plan to fix review or Quality Gate findings keeps the
        # approved images (a new design each time moved the target the gate compares against, and asked again).
        return design and design['requirements_version'] == (task['current_requirements_version'] or 0) \
            and design['config_id'] == task['config_id']

    def required(self, uow, task, text='', files=()):
        # The gate found UI files in the integrated diff of a backend-only scope (T-18): a design is now required, or
        # REQUEST_UI_DESIGN would be refused and nothing could satisfy the gate.
        uow.cur.execute('SELECT requirements FROM quality_gate_evaluations WHERE task_id = %s '
                        'ORDER BY evaluated_at DESC LIMIT 1', (task['id'],))
        evaluation = uow.cur.fetchone()
        if evaluation and any(r.get('name') == 'ui_design' and r.get('status') == 'FAIL' for r in evaluation['requirements']):
            if not task.get('ui_design_required'):
                uow.cur.execute('UPDATE tasks SET ui_design_required = true WHERE id = %s', (task['id'],))
                task['ui_design_required'] = True
            return True
        uow.cur.execute('SELECT title, description, estimated_scope FROM subtasks WHERE task_id = %s '
                        "AND plan_version = %s AND state <> 'CANCELLED'",
                        (task['id'], task['current_plan_version'] or 0))
        subtasks = uow.cur.fetchall()
        scope = list(files) + [p for subtask in subtasks for p in (subtask['estimated_scope'] or {}).get('files', [])]
        uow.cur.execute('SELECT r.source, a.path FROM requirement_versions r JOIN artifacts a ON a.id = r.artifact_id '
                        'WHERE r.task_id = %s AND r.version = %s',
                        (task['id'], task['current_requirements_version'] or 0))
        requirements = uow.cur.fetchone()
        human_text = None
        if requirements and requirements['source'] == 'USER':
            human_text = self.ctx.artifacts.read(requirements['path']).decode('utf-8')
        else:
            uow.cur.execute("SELECT 1 FROM requirement_versions WHERE task_id = %s AND source = 'USER' "
                            'AND version <= %s LIMIT 1', (task['id'], task['current_requirements_version'] or 0))
            if not uow.cur.fetchone():
                uow.cur.execute("SELECT data->'ui_design_requested' AS explicit FROM events "
                                "WHERE task_id = %s AND type = 'TASK_CREATED' ORDER BY seq LIMIT 1", (task['id'],))
                creation = uow.cur.fetchone()
                # Preserve an explicit checkbox, and stay conservative for older tasks with unknown provenance.
                if not task.get('ui_design_required') or (creation and creation['explicit'] is False):
                    uow.cur.execute('SELECT path FROM artifacts WHERE id = %s', (task['original_request_artifact_id'],))
                    original = uow.cur.fetchone()
                    human_text = self.ctx.artifacts.read(original['path']).decode('utf-8') if original else ''
        if human_text is not None and backend_scope_declared(human_text, scope):
            if task.get('ui_design_required'):
                uow.cur.execute('UPDATE tasks SET ui_design_required = false WHERE id = %s', (task['id'],))
                task['ui_design_required'] = False
            uow.cur.execute("SELECT 1 FROM events WHERE task_id = %s AND type = 'UI_SCOPE_CONFIRMED' "
                            "AND data->>'requirements_version' = %s LIMIT 1",
                            (task['id'], str(task['current_requirements_version'])))
            if not uow.cur.fetchone():
                record_event(uow.cur, 'UI_SCOPE_CONFIRMED', actor='control-plane', project_id=task['project_id'],
                             task_id=task['id'], summary='human requirements explicitly exclude UI changes; backend files only',
                             data={'requirements_version': task['current_requirements_version'], 'files': scope},
                             pending=uow.events)
            return False
        if not task.get('ui_design_required'):
            uow.cur.execute('SELECT path FROM artifacts WHERE id = %s', (task['original_request_artifact_id'],))
            original = uow.cur.fetchone()
            request = self.ctx.artifacts.read(original['path']).decode('utf-8') if original else ''
            plan_text = '\n'.join(s['title'] + ' ' + s['description'] for s in subtasks)
            if not detects_ui(request + '\n' + text + '\n' + plan_text, scope):
                return False
            uow.cur.execute('UPDATE tasks SET ui_design_required = true WHERE id = %s', (task['id'],))
            task['ui_design_required'] = True
        return True

    def ensure_approved(self, uow, task, text='', files=()):
        if not self.required(uow, task, text, files):
            return None
        design = self.current(uow, task)
        if not self.matches(design, task) or design['state'] != 'APPROVED':
            raise Conflict('frontend implementation requires the current pen.dev design images and human UI_DESIGN approval')
        # Re-read the immutable package and verify the same content before every launch/dispatch.
        self.package(uow, design)
        return design

    def _read(self, uow, design, name):
        """One file of a design, checked against its manifest entry: (artifact id, content, media type)."""
        expected = design['manifest'].get(name)
        if not expected:
            raise Conflict(f'design package is missing {name}')
        uow.cur.execute('SELECT * FROM artifacts WHERE id = %s AND task_id = %s AND purged_at IS NULL',
                        (expected['artifact_id'], design['task_id']))
        artifact = uow.cur.fetchone()
        if not artifact:
            raise Conflict('design artifact is unavailable')
        try:
            content = self.ctx.artifacts.read(artifact['path'])
        except (OSError, ValueError) as exc:
            raise Conflict('design artifact is unavailable') from exc
        if sha256_hex(content) != expected['sha256'] or len(content) != expected['size_bytes']:
            raise Conflict('design artifact differs from the approved package')
        return artifact['id'], content, attachments.check(name, content).media_type

    def package(self, uow, design):
        refs = []
        for name in PACKAGE:
            artifact_id, content, media_type = self._read(uow, design, name)
            refs.append({'artifact_id': str(artifact_id), 'name': 'ui-' + name,
                         'sha256': design['manifest'][name]['sha256'], 'size_bytes': len(content), 'media_type': media_type})
        if sum(r["size_bytes"] for r in refs) > attachments.MAX_TOTAL_BYTES:
            raise Conflict("UI design package exceeds the attachment size limit")
        return refs

    def prepare(self, uow, task):
        uow.cur.execute('UPDATE tasks SET ui_design_required = true WHERE id = %s', (task['id'],))
        task['ui_design_required'] = True
        previous = self.current(uow, task)
        if self.matches(previous, task) and previous['state'] in ('PREPARING', 'PENDING', 'APPROVED'):
            return previous, False
        uow.cur.execute("DELETE FROM pending_launches WHERE task_id = %s AND request->>'role' = 'DEVELOPER'", (task['id'],))
        self.approvals.invalidate_open(uow, project_id=task['project_id'], task_id=task['id'],
                                       action=ApprovalAction.UI_DESIGN, reason='new UI design version')
        uow.cur.execute("UPDATE ui_designs SET state = 'SUPERSEDED' WHERE task_id = %s AND state IN ('PREPARING', 'PENDING', 'APPROVED')",
                        (task['id'],))
        uow.cur.execute("INSERT INTO ui_designs (id, task_id, version, requirements_version, plan_version, config_id, state) "
                        "VALUES (%s, %s, %s, %s, %s, %s, 'PREPARING') RETURNING *",
                        (uuid7(), task['id'], previous['version'] + 1 if previous else 1,
                         task['current_requirements_version'] or 0, task['current_plan_version'] or 0, task['config_id']))
        return uow.cur.fetchone(), True

    def subject(self, design):
        return {'kind': 'ui_design', 'design_id': str(design['id']), 'version': design['version'],
                'requirements_version': design['requirements_version'], 'plan_version': design['plan_version'],
                'manifest_hash': hash_value(design['manifest'])}

    def _request_approval(self, uow, task, design):
        uow.cur.execute('SELECT effective_hash FROM project_configs WHERE id = %s', (task['config_id'],))
        config_hash = uow.cur.fetchone()['effective_hash']
        approval = self.approvals.request(uow, action=ApprovalAction.UI_DESIGN, project_id=task['project_id'], task_id=task['id'],
                                         subject=self.subject(design), config_hash=config_hash, requested_by='ui-designer',
                                         summary=f"{task['key']}: approve UI design v{design['version']} (desktop and mobile images)",
                                         from_task_state=task['state'])
        uow.cur.execute("UPDATE ui_designs SET state = 'PENDING', approval_id = %s WHERE id = %s", (approval['id'], design['id']))
        if S(task['state']) in ACTIVE_STATES:
            self.tasks.transition(uow, task, S.APPROVAL_REQUIRED, trigger=Trigger.SYSTEM, actor='ui-designer',
                                  reason='review the UI design images before frontend implementation')
        return approval

    def finished(self, uow, execution, state, failure):
        design_id = (execution.get('spec') or {}).get('purpose', {}).get('ui_design')
        if not design_id:
            return
        uow.cur.execute('SELECT * FROM ui_designs WHERE id = %s FOR UPDATE', (design_id,))
        design = uow.cur.fetchone()
        if not design or design['state'] != 'PREPARING' or design['execution_id'] != execution['id']:
            return
        uow.cur.execute('SELECT * FROM tasks WHERE id = %s FOR UPDATE', (design['task_id'],))
        task = uow.cur.fetchone()
        if not self.matches(design, task) or S(task['state']) not in ACTIVE_STATES:
            uow.cur.execute("UPDATE ui_designs SET state = 'SUPERSEDED' WHERE id = %s", (design['id'],))
            return
        if not self.required(uow, task):
            uow.cur.execute("UPDATE ui_designs SET state = 'SUPERSEDED', feedback = %s WHERE id = %s",
                            ('Human requirements explicitly exclude UI changes; backend scope confirmed', design['id']))
            uow.cur.execute('UPDATE tasks SET step_requested = true WHERE id = %s', (task['id'],))
            return
        uow.cur.execute('SELECT * FROM executions WHERE id = %s', (execution['id'],))
        row = uow.cur.fetchone()
        result = row['result'] or {}
        try:
            if state != 'SUCCEEDED' or result.get('status') != 'completed':
                # The reason (error_max_turns), not only the class (TASK): it is the feedback the next version reads.
                raise Conflict(result.get('blocked_reason') or row['failure_reason'] or failure
                               or 'pen.dev design execution did not complete')
            uow.cur.execute('SELECT id, path, sha256, size_bytes FROM artifacts WHERE task_id = %s AND kind = %s',
                            (task['id'], f"executions/{execution['id']}"))
            artifacts = {a['path'].rsplit('/', 1)[-1][37:]: a for a in uow.cur.fetchall()}
            manifest = {}
            for name in COLLECTED:
                artifact = artifacts.get('design__' + name)
                if not artifact:
                    raise Conflict(f'pen.dev did not produce design/{name}')
                content = self.ctx.artifacts.read(artifact['path'])
                checked = attachments.check(name, content)
                saved = self.ctx.artifacts.write(uow.cur, project_id=task['project_id'], task_id=task['id'],
                                                 kind=f"ui-designs/{design['id']}", name=name,
                                                 content=content, media_type=checked.media_type)
                manifest[name] = {'artifact_id': str(saved.id), 'sha256': saved.sha256, 'size_bytes': saved.size_bytes}
            design['manifest'] = manifest
            self.package(uow, design)
            self._read(uow, design, CHANGES)
            uow.cur.execute('UPDATE ui_designs SET manifest = %s WHERE id = %s', (jsonb(manifest), design['id']))
        except (Conflict, ValueError, OSError) as exc:
            uow.cur.execute("UPDATE ui_designs SET state = 'FAILED', feedback = %s WHERE id = %s", (str(exc)[:1000], design['id']))
            if self.recover_failure:
                try:
                    with uow.cur.connection.transaction():
                        if self.recover_failure(uow, task, design, row, str(exc)):
                            return
                except (ApiError, ValueError, OSError):
                    pass  # recovery cannot prevent collection of the original failure
            self.tasks.transition(uow, task, S.BLOCKED, trigger=Trigger.SYSTEM, actor='ui-designer', reason=str(exc)[:300])
            return
        self._request_approval(uow, task, design)

    def _decision(self, uow, approval, approved, principal):
        uow.cur.execute('SELECT * FROM tasks WHERE id = %s FOR UPDATE', (approval['task_id'],))
        task = uow.cur.fetchone()
        design = self.current(uow, task)
        if not design or str(design['id']) != approval['subject'].get('design_id') or design['approval_id'] != approval['id']:
            raise Conflict('this UI design was superseded')
        if not self.matches(design, task) or S(task['state']) in (S.DONE, S.CANCELLED, S.FAILED):
            raise Conflict('requirements, plan or task changed; generate a new UI design')
        if approved:
            if not set(shown(design)) <= set(design['delivered_images']):
                raise Conflict('the UI images (changes, desktop and mobile) must be delivered or explicitly reviewed before approval')
            self.package(uow, design)
            if CHANGES in design['manifest']:
                self._read(uow, design, CHANGES)
            uow.cur.execute('SELECT effective_hash FROM project_configs WHERE id = %s', (task['config_id'],))
            if not self.approvals.consume(uow, approval, subject=self.subject(design),
                                          config_hash=uow.cur.fetchone()['effective_hash']):
                raise Conflict('UI design approval no longer matches')
        uow.cur.execute('UPDATE ui_designs SET state = %s, feedback = %s WHERE id = %s',
                        ('APPROVED' if approved else 'REJECTED', approval.get('decision_note'), design['id']))
        if task['state'] == S.APPROVAL_REQUIRED and task['resume_state']:
            self.tasks.transition(uow, task, S(task['resume_state']), trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason='UI design approved' if approved else 'UI design rejected; revise the proposal')
        record_event(uow.cur, 'ORCHESTRATOR_INPUT', actor=principal.value, project_id=task['project_id'], task_id=task['id'],
                     summary=('UI design approved; implement this exact version' if approved else
                              'UI design rejected; create a new proposal: ' + (approval.get('decision_note') or ''))[:300], pending=uow.events)
        uow.cur.execute('UPDATE tasks SET step_requested = true WHERE id = %s', (task['id'],))

    def _renew(self, uow, expired, principal):
        uow.cur.execute('SELECT * FROM tasks WHERE id = %s FOR UPDATE', (expired['task_id'],))
        task = uow.cur.fetchone()
        design = self.current(uow, task)
        if not self.matches(design, task) or design['state'] != 'PENDING' or design['approval_id'] != expired['id']:
            raise Conflict('UI design was superseded')
        self.package(uow, design)
        return self._request_approval(uow, task, design)

    def summary(self, uow, design):
        """The reviewer summary of a design's own design.md; empty when it has none or the file is unavailable."""
        ref = (design.get('manifest') or {}).get('design.md') if design else None
        if not ref:
            return ''
        uow.cur.execute('SELECT path FROM artifacts WHERE id = %s AND purged_at IS NULL', (ref['artifact_id'],))
        artifact = uow.cur.fetchone()
        try:
            content = self.ctx.artifacts.read(artifact['path']) if artifact else b''
        except (OSError, ValueError):
            return ''
        if sha256_hex(content) != ref['sha256']:
            return ''
        return review_summary(content.decode('utf-8', 'replace'))

    def file(self, uow, task, design_id, name):
        design = self.current(uow, task)
        if not self.matches(design, task) or str(design['id']) != str(design_id) or name not in COLLECTED \
                or name not in design['manifest']:
            raise NotFound('UI design file not found')
        _, content, media_type = self._read(uow, design, name)
        return content, media_type
