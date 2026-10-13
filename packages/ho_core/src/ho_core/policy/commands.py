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
    # Container tools run as a command, or their socket. A tool run through a wrapper (`env X=1 docker`, `xargs -I {}
    # docker`, `bash -lc "docker ps"`) is found by `_executables` below; the word inside an ordinary argument
    # (`grep -i docker`, `find -iname "docker-compose*"`) is not container control (T-55).
    _rule("CMD-H07", H, r"^(?:\S*/)?(?:docker(?:-compose)?|podman|nerdctl|kubectl|helm)\b|docker\.sock", "Container or cluster control"),
    _rule("CMD-H08", H, r"\b(terraform\s+(apply|destroy)|pulumi\s+(up|destroy)|cdk\s+deploy|serverless\s+deploy)\b", "Infrastructure change"),
    _rule("CMD-H09", H, r"\b(sudo|su\s|chmod\s+(-R\s+)?[0-7]*7[0-7]{2}\b|chown\s+-R|setcap|mount\s)", "Privilege or permission change"),
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

_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_ORDER = {S: 0, C: 1, H: 2}


CONTAINER_TOOLS = frozenset({"docker", "docker-compose", "podman", "nerdctl", "kubectl", "helm"})
_WRAPPERS = frozenset({"env", "xargs", "exec", "nohup", "sudo", "watch", "timeout", "nice", "time", "command", "stdbuf",
                       "ionice", "setsid", "chroot", "doas", "unbuffer", "parallel"})
_OPTION_ARGS = frozenset({"-I", "-i", "-n", "-P", "-L", "-s", "-d", "-u", "-g", "-E", "-C", "-k", "-a", "--replace"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done"})


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


def _executables(text: str, depth: int = 0) -> list[str]:
    """Every program a line runs: each command's first word, through wrappers and their options, and inside
    `sh -c` scripts."""
    found: list[str] = []
    for tokens in _commands(text):
        i = 0
        while i < len(tokens):
            name = tokens[i].rsplit("/", 1)[-1]
            if name in _KEYWORDS:  # `if docker ps; then`, `do docker ps; done`: the program comes after the keyword
                i += 1
                continue
            found.append(name)
            if name in _SHELLS and depth < 3:
                for j in range(i + 1, len(tokens) - 1):
                    if tokens[j].startswith("-") and not tokens[j].startswith("--") and "c" in tokens[j]:
                        found += _executables(tokens[j + 1], depth + 1)
                        break
                break
            if name not in _WRAPPERS:
                break
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or re.match(r"^\w+=", tokens[i]) or tokens[i].isdigit()
                                       or tokens[i] in ("{}", "%")):
                i += 2 if tokens[i] in _OPTION_ARGS else 1
    return found


def _classify_segment(segment: str) -> Classification:
    if CONTAINER_TOOLS & set(_executables(segment)):
        return next(Classification(r.command_class, r.rule_id, r.summary) for r in _RULES if r.rule_id == "CMD-H07")
    for rule in _RULES:
        if rule.pattern.search(segment):
            return Classification(rule.command_class, rule.rule_id, rule.summary)
    return Classification(C, "CMD-C99", "Unrecognized command")


def classify(command: str) -> Classification:
    """Classify a shell command line; the riskiest segment decides."""
    text = command.strip()
    if not text:
        return Classification(C, "CMD-C99", "Empty command")
    # The whole line is checked too, for patterns that span separators, and for container tools run anywhere in it
    # (through wrappers or shell scripts, with their quoting intact).
    if CONTAINER_TOOLS & set(_executables(text)):
        return next(Classification(r.command_class, r.rule_id, r.summary) for r in _RULES if r.rule_id == "CMD-H07")
    results = [_classify_segment(_strip_prefix(text))]
    results += [_classify_segment(_strip_prefix(s)) for s in _SEPARATORS.split(text) if s]
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
    if re.search(r"\$\(|`|<\(|>\(|>|<|(?<!&)&(?!&)|\beval\b|\bsource\b", text):
        return False
    absent = False
    for tokens in _commands(text):
        # Through wrappers (`env X=1 docker`, `timeout 5 sudo …`): the first program that is absent fails the command.
        runs = next((p for p in _executables(shlex.join(tokens)) if p not in _WRAPPERS - {"sudo"}), None)
        if runs in CONTAINER_TOOLS | {"sudo"}:
            absent = True
            continue
        if _classify_segment(_strip_prefix(shlex.join(tokens))).command_class != S:
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
