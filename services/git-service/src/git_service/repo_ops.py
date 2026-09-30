"""Local repository operations (MASTER_SPEC sections 36-38, 41; ARCHITECTURE.md sections 7.4 and 8).

Trust model (SECURITY_MODEL.md section 9):
- The user's repository is trusted configuration but its working tree is the
  user's: nothing here overwrites uncommitted work.
- Workspace clones are created here and then handed to model-controlled code, so
  after creation they are hostile. They are only *read from* with a hardened
  fetch: no checkout, status, diff, or hooks ever run inside them.
- Integration and merges are computed in the object database (`merge-tree`,
  `commit-tree`) without a working tree, and refs are moved with compare-and-swap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ho_core.gitpolicy import ChangeLevel, FileChange, classify_divergence, parse_hunks, valid_branch, valid_sha, valid_workspace

from .gitcmd import GitError, git, run, try_git

WORKTREES = Path(".hermes") / "worktrees"
EXCLUDES = (".hermes/worktrees/", ".hermes/generated/")
AGENT_IDENTITY = ("Hermes Agent", "agent@hermes.local")
MAX_COMMITS = 200
MAX_FILES = 2000
_FETCH_TIMEOUT = 300
_REF = re.compile(r"^refs/(heads|hermes)/[A-Za-z0-9._/-]{1,200}$")


class Refused(RuntimeError):
    """The operation would violate a Git rule or would overwrite someone's work (HTTP 409)."""


@dataclass
class Workspace:
    name: str
    path: Path  # absolute path of the clone

    @property
    def collected_ref(self) -> str:
        return f"refs/hermes/workspaces/{self.name}"


def workspace(repo: Path, name: str) -> Workspace:
    if not valid_workspace(name):
        raise Refused(f"invalid workspace name {name!r}")
    parent = repo / WORKTREES
    for component in (repo / ".hermes", parent):
        if component.is_symlink():
            raise Refused(f"{component.relative_to(repo)} must not be a symbolic link")
    return Workspace(name, parent / name)


def resolve_commit(repo: Path, ref: str) -> str:
    if not (valid_sha(ref) or _REF.match(ref) or valid_branch(ref)):
        raise Refused(f"invalid ref {ref!r}")
    sha = try_git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    if not sha:
        raise Refused(f"{ref} does not name a commit")
    return sha


def _full_ref(repo: Path, ref: str) -> str:
    if _REF.match(ref):
        return ref
    if valid_branch(ref) and run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{ref}", ok_codes=(0, 1)).returncode == 0:
        return f"refs/heads/{ref}"
    raise Refused(f"{ref} is not a branch or platform ref")


def _ensure_excluded(repo: Path) -> bool:
    """Keep workspaces out of the user's `git status`: add them to .git/info/exclude if needed."""
    ignored = run(repo, "check-ignore", "-q", "--no-index", ".hermes/worktrees/probe", ok_codes=(0, 1)).returncode == 0
    if ignored:
        return False
    info = repo / ".git" / "info"
    if info.is_symlink() or (info / "exclude").is_symlink():
        raise Refused(".git/info/exclude must not be a symbolic link")
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = [e for e in EXCLUDES if e not in existing.splitlines()]
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(("" if existing.endswith("\n") or not existing else "\n") +
                     "# Hermes Orchestrator workspaces\n" + "".join(f"{e}\n" for e in lines))
    return True


# ------------------------------------------------------------------ workspaces


def prepare_workspace(repo: Path, name: str, branch: str, base_ref: str, pin_ref: str | None = None) -> dict[str, Any]:
    """Create an isolated clone with one branch at `base_ref` (section 36, AD-06).

    The clone has no remote and only the objects reachable from the base: it
    cannot see the user's other branches or other tasks' collected work.
    """
    ws = workspace(repo, name)
    if ws.path.exists() or ws.path.is_symlink():
        raise Refused(f"workspace {name} already exists")
    if not valid_branch(branch):
        raise Refused(f"invalid branch name {branch!r}")
    full = _full_ref(repo, base_ref)
    base_sha = resolve_commit(repo, full)
    if pin_ref:
        # The task's base, fixed once so every later workspace of the task starts from it.
        if not re.match(r"^refs/hermes/tasks/T-[0-9]+/base$", pin_ref):
            raise Refused("invalid base pin ref")
        run(repo, "update-ref", pin_ref, base_sha, "")
    excluded = _ensure_excluded(repo)
    ws.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(repo, "init", "-q", "-b", branch, str(ws.path))
        run(ws.path, "fetch", "-q", "--no-tags", "--update-head-ok", str(repo), f"+{full}:refs/heads/{branch}",
            protocols="file", trusted=(repo,), timeout=_FETCH_TIMEOUT)
        fetched = git(ws.path, "rev-parse", f"refs/heads/{branch}")
        if fetched != base_sha:
            raise Refused(f"{base_ref} moved while the workspace was prepared")
        run(ws.path, "reset", "-q", "--hard", base_sha, timeout=_FETCH_TIMEOUT)
        for key, value in (("user.name", AGENT_IDENTITY[0]), ("user.email", AGENT_IDENTITY[1])):
            run(ws.path, "config", key, value)
    except Exception:
        _remove_tree(ws.path)
        raise
    return {"workspace": name, "path": str(ws.path.relative_to(repo)), "branch": branch, "base_sha": base_sha,
            "excluded_added": excluded}


def _check_clone(ws: Workspace) -> Path:
    """Refuse clones rearranged to read another repository through Git's indirections."""
    git_dir = ws.path / ".git"
    if ws.path.is_symlink() or not ws.path.is_dir():
        raise Refused(f"workspace {ws.name} is missing")
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise Refused(f"workspace {ws.name} has no plain .git directory (gitfile or link)")
    for relative in ("objects/info/alternates", "objects/info/http-alternates", "commondir"):
        if (git_dir / relative).exists() or (git_dir / relative).is_symlink():
            raise Refused(f"workspace {ws.name} uses {relative}, which could read other repositories")
    for relative in ("objects", "refs", "packed-refs", "config"):
        if (git_dir / relative).is_symlink():
            raise Refused(f"workspace {ws.name} has a symbolic link at .git/{relative}")
    return git_dir


def collect(repo: Path, name: str, branch: str, base_sha: str) -> dict[str, Any]:
    """Hardened fetch of the workspace branch into refs/hermes/workspaces/<name> (section 38 input)."""
    ws = workspace(repo, name)
    git_dir = _check_clone(ws)
    if not valid_branch(branch) or not valid_sha(base_sha):
        raise Refused("invalid branch or base commit")
    run(repo, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(git_dir), f"+refs/heads/{branch}:{ws.collected_ref}",
        protocols="file", trusted=(git_dir, ws.path), timeout=_FETCH_TIMEOUT,
        config=("transfer.fsckObjects=true", "fetch.fsckObjects=true"), env={"GIT_NO_REPLACE_OBJECTS": "1"})
    head = git(repo, "rev-parse", ws.collected_ref)
    descends = run(repo, "merge-base", "--is-ancestor", base_sha, head, ok_codes=(0, 1)).returncode == 0
    return {"workspace": name, "ref": ws.collected_ref, "head_sha": head, "descends_from_base": descends,
            **changes(repo, base_sha, head)}


def changes(repo: Path, base: str, head: str) -> dict[str, Any]:
    log = git(repo, "log", f"--max-count={MAX_COMMITS}", "--format=%H%x1f%an%x1f%s", f"{base}..{head}", "--")
    commits = [dict(zip(("sha", "author", "subject"), line.split("\x1f", 2), strict=False)) for line in log.splitlines() if line]
    files = []
    for line in git(repo, "diff", "--name-status", "-M", base, head, "--").splitlines()[:MAX_FILES]:
        parts = line.split("\t")
        files.append({"status": parts[0], "path": parts[-1]})
    stat = git(repo, "diff", "--shortstat", base, head, "--")
    numbers = [int(n) for n in re.findall(r"(\d+) (?:file|insertion|deletion)", stat)]
    return {"commits": commits, "files": files, "shortstat": stat,
            "insertions": int(m.group(1)) if (m := re.search(r"(\d+) insertion", stat)) else 0,
            "deletions": int(m.group(1)) if (m := re.search(r"(\d+) deletion", stat)) else 0,
            "files_changed": numbers[0] if numbers else 0}


def remove_workspace(repo: Path, name: str) -> dict[str, Any]:
    ws = workspace(repo, name)
    existed = ws.path.exists()
    _remove_tree(ws.path)
    run(repo, "update-ref", "-d", ws.collected_ref, ok_codes=(0, 1))
    return {"workspace": name, "removed": existed}


def _remove_tree(path: Path) -> None:
    """Remove a workspace without following symbolic links planted inside it."""
    import shutil

    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


# ------------------------------------------------------------------ divergence


def _file_changes(repo: Path, *diff_args: str) -> dict[str, FileChange]:
    statuses = git(repo, "diff", "--name-status", "--no-renames", *diff_args, "--")
    result: dict[str, FileChange] = {}
    for line in statuses.splitlines():
        status, _, path = line.partition("\t")
        result[path] = FileChange(path, status[:1])
    hunks = parse_hunks(git(repo, "diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", *diff_args, "--"))
    for path, ranges in hunks.items():
        if path in result:
            result[path].ranges = ranges
    return result


def divergence(repo: Path, *, base_sha: str, head_ref: str, target_branch: str,
               sensitive_paths: list[str], critical_paths: list[str]) -> dict[str, Any]:
    """Classify what the user changed since the task's base against what the task changed (section 37)."""
    if not valid_sha(base_sha) or not valid_branch(target_branch):
        raise Refused("invalid base commit or target branch")
    target_sha = resolve_commit(repo, f"refs/heads/{target_branch}")
    head_sha = resolve_commit(repo, head_ref)
    checked_out = try_git(repo, "symbolic-ref", "--quiet", "HEAD") == f"refs/heads/{target_branch}"
    if checked_out:
        # The user's working tree (committed and uncommitted work) against the task's base.
        human = _file_changes(repo, base_sha)
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        for path in filter(None, untracked):
            human.setdefault(path, FileChange(path, "A"))
        dirty = bool(git(repo, "diff", "--name-only", "HEAD", "--")) or any(untracked)
    else:
        human = _file_changes(repo, base_sha, target_sha)
        dirty = False
    task = _file_changes(repo, base_sha, head_sha)
    level, files = classify_divergence(human, task, sensitive_paths=sensitive_paths, critical_paths=critical_paths)
    if not human:
        level = ChangeLevel.NONE
    return {
        "level": level.value,
        "target_sha": target_sha,
        "head_sha": head_sha,
        "human_commits": int(git(repo, "rev-list", "--count", f"{base_sha}..{target_sha}")),
        "human_files": sorted(human)[:MAX_FILES],
        "uncommitted_changes": dirty,
        "overlapping": [f.as_json() for f in files],
    }


# ----------------------------------------------------------------- integration


def _merge_tree(repo: Path, ours: str, theirs: str) -> tuple[str | None, list[str]]:
    result = run(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs, ok_codes=(0, 1))
    lines = result.stdout.splitlines()
    if result.returncode == 0:
        return lines[0].strip(), []
    return None, sorted({line for line in lines[1:] if line.strip()})


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    return run(repo, "merge-base", "--is-ancestor", a, b, ok_codes=(0, 1)).returncode == 0


def integrate(repo: Path, *, task: str, target_branch: str, heads: list[str]) -> dict[str, Any]:
    """Merge the task's collected workspace heads onto the current target in the object database.

    Nothing is checked out and no repository content runs. On success the result is
    refs/hermes/tasks/<task>/integration; on conflict nothing changes and the
    conflicting files are reported (section 38).
    """
    if not re.match(r"^T-[0-9]+$", task):
        raise Refused("invalid task key")
    target_sha = resolve_commit(repo, f"refs/heads/{target_branch}")
    current = target_sha
    merged = []
    for ref in heads:
        head = resolve_commit(repo, ref)
        if _is_ancestor(repo, head, current):
            merged.append({"ref": ref, "sha": head, "result": "already included"})
            continue
        if _is_ancestor(repo, current, head):
            current = head
            merged.append({"ref": ref, "sha": head, "result": "fast-forward"})
            continue
        tree, conflicts = _merge_tree(repo, current, head)
        if tree is None:
            return {"ok": False, "target_sha": target_sha, "conflicts": conflicts, "conflicting_ref": ref, "merged": merged}
        current = git(repo, "commit-tree", tree, "-p", current, "-p", head, "-m", f"Integrate {ref} for {task}")
        merged.append({"ref": ref, "sha": head, "result": "merged"})
    ref = f"refs/hermes/tasks/{task}/integration"
    run(repo, "update-ref", ref, current)
    return {"ok": True, "target_sha": target_sha, "integration_ref": ref, "integration_sha": current, "merged": merged,
            **changes(repo, target_sha, current)}


def prepare_conflict_workspace(repo: Path, *, name: str, branch: str, target_branch: str, incoming_ref: str) -> dict[str, Any]:
    """A fresh clone at the target with `incoming_ref` merged and its conflicts left for an agent
    to resolve (agent-assisted reconciliation, section 38). Runs `git merge` only in the clone it
    has just created, before any execution can touch it."""
    prepared = prepare_workspace(repo, name, branch, f"refs/heads/{target_branch}")
    ws = workspace(repo, name)
    incoming = _full_ref(repo, incoming_ref)
    run(ws.path, "fetch", "-q", "--no-tags", str(repo), f"+{incoming}:refs/heads/incoming", protocols="file",
        trusted=(repo,), timeout=_FETCH_TIMEOUT)
    result = run(ws.path, "merge", "--no-ff", "--no-commit", "incoming", ok_codes=(0, 1))
    conflicts = git(ws.path, "diff", "--name-only", "--diff-filter=U").splitlines()
    return {**prepared, "incoming_sha": resolve_commit(repo, incoming), "conflicts": conflicts,
            "clean": result.returncode == 0}


# ------------------------------------------------------------------------ merge


def merge_local(repo: Path, *, approval_id: str, task: str, target_branch: str, target_sha: str, head_sha: str,
                method: str) -> dict[str, Any]:
    """Approved merge into a local branch (section 41). Idempotent per approval.

    The target must still be exactly `target_sha`. If the target branch is checked out in
    the user's working tree, the result is applied with a fast-forward that Git refuses
    when it would overwrite uncommitted changes; otherwise the ref moves with compare-and-swap.
    """
    record = f"refs/hermes/merges/{approval_id}"
    done = try_git(repo, "rev-parse", "--verify", "--quiet", record)
    if done:
        return {"merge_sha": done, "target_branch": target_branch, "method": method, "already_merged": True}
    if method not in ("merge", "squash"):
        raise Refused("local repositories support the merge and squash methods; use a pull request for rebase")
    current = resolve_commit(repo, f"refs/heads/{target_branch}")
    if current != target_sha:
        raise Refused(f"{target_branch} moved from the approved commit {target_sha[:12]} to {current[:12]}")
    resolve_commit(repo, head_sha)
    tree, conflicts = _merge_tree(repo, target_sha, head_sha)
    if tree is None:
        raise Refused(f"the approved change no longer merges cleanly: {', '.join(conflicts[:10])}")
    message = f"Merge {task} ({method}, approval {approval_id})"
    parents = ["-p", target_sha] + (["-p", head_sha] if method == "merge" else [])
    commit = git(repo, "commit-tree", tree, *parents, "-m", message)
    if try_git(repo, "symbolic-ref", "--quiet", "HEAD") == f"refs/heads/{target_branch}":
        try:
            run(repo, "merge", "--ff-only", "--no-edit", commit, timeout=_FETCH_TIMEOUT)
        except GitError as exc:
            raise Refused(f"the checkout of {target_branch} has uncommitted changes the merge would overwrite: {exc}") from exc
    else:
        run(repo, "update-ref", "-m", message, f"refs/heads/{target_branch}", commit, target_sha)
    run(repo, "update-ref", record, commit)
    return {"merge_sha": commit, "target_branch": target_branch, "method": method, "already_merged": False}


def refs(repo: Path, names: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in names[:50]:
        if not (_REF.match(name) or valid_branch(name)):
            raise Refused(f"invalid ref {name!r}")
        full = name if name.startswith("refs/") else f"refs/heads/{name}"
        result[name] = try_git(repo, "rev-parse", "--verify", "--quiet", full)
    return result
