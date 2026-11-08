"""Command risk classification (MASTER_SPEC section 24, SECURITY_MODEL.md section 11).

Inside a worker this is advisory: the hard boundary is that high-risk
capabilities are absent from the container. The same classifier is used by
the control plane for commands it is asked to authorize.

Unknown commands are CONTROLLED, never SAFE.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from ..enums import CommandClass


@dataclass(frozen=True)
class Classification:
    command_class: CommandClass
    rule_id: str
    summary: str
    # The text the rule matched, with a little context on each side: what a person checks to tell a real risk from
    # a word that only looks like one (a stored command is cut short and may not contain it).
    match: str = ""


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    command_class: CommandClass
    pattern: re.Pattern[str]
    summary: str


def _rule(rule_id: str, command_class: CommandClass, pattern: str, summary: str) -> _Rule:
    return _Rule(rule_id, command_class, re.compile(pattern, re.IGNORECASE), summary)


H, C, S = CommandClass.HIGH_RISK, CommandClass.CONTROLLED, CommandClass.SAFE

# Evaluated in order; HIGH_RISK rules first so that a dangerous segment wins.
_RULES: tuple[_Rule, ...] = (
    _rule("CMD-H01", H, r"\bgit\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b|\s\+\S)", "Force push"),
    _rule("CMD-H02", H, r"\bgit\s+push\b.*(--delete\b|\s-d\b|\s:\S)", "Remote branch deletion"),
    _rule("CMD-H03", H, r"\bgit\s+(branch\s+-D|filter-branch|filter-repo|update-ref\s+-d)\b", "Destructive Git history change"),
    _rule("CMD-H04", H, r"\bgh\s+(repo\s+delete|pr\s+merge|api\b.*-X\s*DELETE)", "GitHub destructive or merge operation"),
    _rule("CMD-H05", H, r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+(/|~|\$HOME|\.\.|\*)(\s|/?$)", "Mass deletion outside the workspace"),
    _rule("CMD-H06", H, r"\b(drop\s+(database|schema|table)|truncate\s+table|delete\s+from\s+\w+\s*;?\s*$)", "Destructive database statement"),
    # Container tools anywhere in the line (wrappers, shell scripts), or their socket; except in a line that only
    # searches or reads (`git log | grep -i docker`, `find -iname "docker-compose*"`: T-55), see `_search_only`.
    # A tool name as a word of its own (`/usr/bin/docker` counts; `build-docker-image.yaml` in a URL does not).
    _rule("CMD-H07", H, r"(?<![\w.-])(docker(-compose)?|podman|nerdctl|kubectl|helm)(?![\w.-])|docker\.sock",
          "Container or cluster control"),
    _rule("CMD-H08", H, r"\b(terraform\s+(apply|destroy)|pulumi\s+(up|destroy)|cdk\s+deploy|serverless\s+deploy)\b", "Infrastructure change"),
    # sudo and su as programs only: not Odoo's `.sudo()` nor the Spanish word "su" (T-12 to T-15).
    _rule("CMD-H09", H, r"((?<![\w.])sudo\b(?!\s*\()|(?:^|[;&|(]\s*)su(?:\s|$)|\bchmod\s+(-R\s+)?[0-7]*7[0-7]{2}\b|\bchown\s+-R|"
                        r"\bsetcap\b|\bmount\s+(?!\d?>|[|;&])\S)", "Privilege or permission change"),
    _rule("CMD-H10", H, r"\b(aws|gcloud|az)\s+\S*\s*(delete|remove|rm|destroy|deploy)\b", "Cloud destructive or deploy operation"),
    _rule("CMD-H11", H, r"(/run/secrets|/run/ho-credentials|/run/ho/secrets|\.ssh/|\.aws/credentials|\.config/gh|\.codex/auth|\.claude/\.credentials)", "Credential material access"),
    _rule("CMD-C01", C, r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|ci|update|upgrade)\b", "Package install"),
    _rule("CMD-C02", C, r"\b(pip|pip3|uv|poetry|pipenv)\s+(install|add|sync|lock|update)\b", "Package install"),
    _rule("CMD-C03", C, r"\b(apt|apt-get|apk|brew|gem|cargo\s+install|go\s+install|composer\s+(require|install|update)|flutter\s+pub\s+(get|upgrade)|dart\s+pub\s+(get|upgrade))\b", "Package install"),
    _rule("CMD-C04", C, r"\b(curl|wget|git\s+clone|git\s+fetch|git\s+pull|scp|rsync)\b", "Download or remote transfer"),
    _rule("CMD-C05", C, r"\bgit\s+push\b", "Push (only Git Service may push)"),
    _rule("CMD-C06", C, r"\b(export|setx?)\s+\w+=|\benv\s+-[iu]\b", "Environment change"),
    _rule("CMD-C07", C, r"\brm\s+-[a-z]*r", "Recursive deletion"),
    _rule("CMD-C08", C, r"\b(psql|mysql|mongosh|redis-cli|sqlite3)\b", "Database client"),
    _rule("CMD-S01", S, r"^(ls|cat|head|tail|wc|grep|rg|find|fd|tree|pwd|echo|stat|file|diff|sort|uniq|cut|jq|yq|which|true)\b", "Read-only inspection"),
    _rule("CMD-S02", S, r"^git\s+(status|diff|log|show|blame|branch(\s+--list)?|rev-parse|ls-files|add|commit|restore|switch|checkout\s+-b|stash)\b", "Local Git operation"),
    _rule("CMD-S03", S, r"^(npm|pnpm|yarn|bun)\s+(test|run\s+(test|lint|build|typecheck|format|check)\S*)\b", "Build, test, or lint"),
    _rule("CMD-S04", S, r"^(pytest|python3?\s+-m\s+(pytest|unittest|mypy|ruff)|ruff|mypy|tox|nox|go\s+(test|build|vet)|cargo\s+(test|build|check|clippy|fmt)|mvn\s+(test|verify|package)|gradle\w*\s+(test|build|check)|flutter\s+(test|analyze)|dart\s+(test|analyze)|phpunit|composer\s+test|tsc|eslint|prettier\s+--check|make\s+(test|lint|build|check))\b", "Build, test, or lint"),
)

# Rules that judge program words, so the data a command only passes along (a quoted string, a heredoc written to a
# file, Python source given to `python -c`) cannot trigger them: test strings like "Docker skills a bonus" did (T-15).
# What a shell runs (`sh -c '...'`, `eval '...'`, a heredoc fed to a shell) is still checked.
_PROGRAM_RULES = frozenset({"CMD-H07", "CMD-H09"})
_RUNS_SHELL = re.compile(r"(\b(ba|z|da)?sh\s+(-\w*\s+)*-\w*c\w*\s*|\beval\s+)$")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)


def _without_data(text: str) -> str:
    """The line with the data it only passes along blanked out (see _PROGRAM_RULES)."""
    def heredoc(found: re.Match[str]) -> str:
        line_start = text.rfind("\n", 0, found.start()) + 1
        head = text[line_start:found.start()]
        if re.search(r"(^|[;&|(]\s*)(\S*/)?(ba|z|da)?sh\b", head):  # fed to a shell: those are commands
            return found.group(0)
        return f"<<{found.group(2)}{found.group(3)}\n{found.group(2)}"
    text = _HEREDOC.sub(heredoc, text)
    out, last = [], 0
    for found in _QUOTED.finditer(text):
        out.append(text[last:found.start()])
        quoted = found.group(0)
        if _RUNS_SHELL.search(text[:found.start()]):
            # A script: its own commands stay, the data inside it is blanked too (`bash -lc "rg 'sudo\\('"`).
            inner = quoted[1:-1].replace('\\"', '"') if quoted[0] == '"' else quoted[1:-1]
            out.append(quoted[0] + _without_data(inner) + quoted[0])
        else:
            out.append("''")
        last = found.end()
    return "".join(out) + text[last:]


_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_ORDER = {S: 0, C: 1, H: 2}


CONTAINER_TOOLS = frozenset({"docker", "docker-compose", "podman", "nerdctl", "kubectl", "helm"})
_READ_ONLY = frozenset({"ls", "cat", "head", "tail", "wc", "grep", "echo", "pwd", "true", "which", "stat", "cut"})


_COMMAND_BREAKS = frozenset({";", "&&", "||", "|", "&", "\n", "(", ")", "|&", ";;"})


def _commands(text: str) -> list[list[str]]:
    """The simple commands of a line as token lists, quoting preserved (`bash -lc "exec docker ps"` keeps its script)."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token in _COMMAND_BREAKS or set(token) <= set(";&|()"):
                commands.append([])
            else:
                commands[-1].append(token)
    except ValueError:  # unbalanced quotes: fall back to words
        commands = [text.split()]
    return [c for c in commands if c]


# Programs whose options cannot run other programs (not rg: --pre; not git diff: external diff and textconv).
_SEARCH = frozenset({"grep", "ls", "cat", "head", "tail", "wc", "echo", "fc-list", "which"})
_GIT_READ = frozenset({"log", "status", "ls-files"})
_GIT_SAFE_OPTION = re.compile(r"^(--all|--oneline|--graph|--decorate|--short|--porcelain|-s|-n|-\d+|--max-count=\d+|"
                              r"--format=\S*|--pretty=\S*|--)$")


def _search_only(text: str) -> bool:
    """A line made only of searches and reads, where container words can only be arguments (no wrapper, shell,
    substitution, or `find -exec`)."""
    if re.search(r"\$\(|`|<\(|>\(|\beval\b|[\n\r]|docker\.sock|/var/run/docker", text):  # the socket is never an argument
        return False
    commands = _commands(text)
    for tokens in commands:
        name = tokens[0]
        if name == "find" and not set(tokens) & {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}:
            continue
        if name == "git" and len(tokens) > 1 and tokens[1] in _GIT_READ \
                and all(_GIT_SAFE_OPTION.match(t) or not t.startswith("-") for t in tokens[2:]):
            continue
        if name not in _SEARCH:
            return False
    return bool(commands)


def _context(text: str, found: re.Match[str], around: int = 40) -> str:
    start, end = max(0, found.start() - around), min(len(text), found.end() + around)
    excerpt = " ".join(text[start:end].split())
    return ("…" if start else "") + excerpt + ("…" if end < len(text) else "")


def _classify_segment(segment: str, *, rules: frozenset[str] | None = None,
                      skip: frozenset[str] = frozenset()) -> Classification | None:
    for rule in _RULES:
        if rule.rule_id in skip or (rules is not None and rule.rule_id not in rules):
            continue
        if rule.rule_id == "CMD-H07" and _search_only(segment):
            continue
        found = rule.pattern.search(segment)
        if found:
            return Classification(rule.command_class, rule.rule_id, rule.summary, _context(segment, found))
    return None if rules is not None else Classification(C, "CMD-C99", "Unrecognized command")


def classify(command: str) -> Classification:
    """Classify a shell command line; the riskiest segment decides."""
    text = command.strip()
    if not text:
        return Classification(C, "CMD-C99", "Empty command")
    # The whole line is checked too, for patterns that span separators.
    results = [_classify_segment(_strip_prefix(s), skip=_PROGRAM_RULES)
               for s in [text, *(s for s in _SEPARATORS.split(text) if s)]]
    # Program rules see the line without the data it only passes along.
    code = _without_data(text)
    results += [r for s in [code, *(s for s in _SEPARATORS.split(code) if s)]
                if (r := _classify_segment(_strip_prefix(s), rules=_PROGRAM_RULES)) is not None]
    # Every segment must be SAFE for the line to be SAFE; the riskiest one decides.
    worst = max(results, key=lambda r: _ORDER[r.command_class])
    # Command substitution and eval can hide anything.
    if worst.command_class == S and re.search(r"\$\(|`|\beval\b|\bsource\b|\b(ba)?sh\s+-c\b", text):
        return Classification(C, "CMD-C98", "Command substitution or eval")
    return worst


def provably_blocked(command: str) -> bool:
    """Whether a flagged command can have had no effect: each of its simple commands either runs a program absent from
    every worker image (a container tool, or sudo: AD-14) or is a read-only inspection. Anything else in the line
    (chmod, su, a database client, an unknown program, a substitution, a redirection to a file, a background job)
    may have had an effect, so the command is not provably blocked (T-55, Codex review)."""
    text = re.sub(r"\s[12]?>&[12]|\s[12]?>\s*/dev/null", " ", command)  # harmless redirections
    if re.search(r"\$\(|`|<\(|>\(|>|<|(?<!&)&(?!&)|\beval\b|\bsource\b|[\n\r]", text):  # a newline separates commands
        return False
    absent = False
    for tokens in _commands(text):
        # The command itself is the absent program, by bare name: no wrapper (whose options can run programs) and no
        # path (`/workspace/docker` could exist). Whatever follows sudo never runs, since sudo is absent.
        if tokens[0] in CONTAINER_TOOLS | {"sudo"}:
            absent = True
            continue
        # Only programs that never write, with nothing flagged in their arguments (not `find`, which can -delete).
        if tokens[0] not in _READ_ONLY or _classify_segment(_strip_prefix(shlex.join(tokens))).command_class != S:
            return False
    return absent


def _strip_prefix(segment: str) -> str:
    """Drop leading VAR=value assignments and common wrappers such as `time`."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return segment
    while tokens and (re.match(r"^\w+=", tokens[0]) or tokens[0] in {"time", "nice", "timeout", "command"}):
        tokens.pop(0)
        if tokens and tokens[0].isdigit():  # timeout <seconds>
            tokens.pop(0)
    return " ".join(tokens) if tokens else segment
