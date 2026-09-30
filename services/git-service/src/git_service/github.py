"""Remote operations for GitHub repositories (MASTER_SPEC sections 39-41).

Only Git Service holds the GitHub credential: the `gh` configuration directory
(GH_CONFIG_DIR, the `gh-config` volume) created by `make auth-github`. Git uses it
through `gh auth git-credential`; SSH remotes on github.com are rewritten to HTTPS
because no SSH key is available here.

Rules enforced here, independently of the control plane:
- push only platform branches (the project's prefix), never a protected branch;
- never `--force`; updating an existing platform branch uses `--force-with-lease`
  bound to the commit the platform last pushed;
- delete only platform branches;
- merge only with a verified merge authorization, bound to the PR head
  (`gh pr merge --match-head-commit`), never with `--admin` or `--auto`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ho_core.gitpolicy import check_task_branch, valid_sha

from .gitcmd import GitError, git, run, sanitize_remote, try_git
from .repo_ops import Refused

_GITHUB = re.compile(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_GH_TIMEOUT = 120


def _allow_file_remotes() -> bool:
    # Test setting only: lets the suite use a local bare repository as "origin".
    return os.environ.get("HO_GIT_ALLOW_FILE_REMOTES") == "1"


def remote_info(repo: Path) -> dict[str, Any]:
    url = try_git(repo, "config", "--get", "remote.origin.url")
    if not url:
        return {"kind": "local", "remote": None}
    match = _GITHUB.match(url)
    if match:
        return {"kind": "github", "remote": sanitize_remote(url), "repo": f"{match['owner']}/{match['repo']}"}
    if _allow_file_remotes() and (url.startswith("/") or url.startswith("file://")):
        return {"kind": "github", "remote": url, "repo": os.environ.get("HO_TEST_GH_REPO", "test/test")}
    return {"kind": "other", "remote": sanitize_remote(url)}


def _github(repo: Path) -> dict[str, Any]:
    info = remote_info(repo)
    if info["kind"] != "github":
        raise Refused("this operation needs an origin remote on github.com")
    return info


def _git_remote(repo: Path, *args: str, timeout: int = _GH_TIMEOUT) -> str:
    protocols = "https,file" if _allow_file_remotes() else "https"
    config = (
        "credential.helper=",  # ignore any helper from the user's configuration
        "credential.https://github.com.helper=!gh auth git-credential",
        "url.https://github.com/.insteadOf=git@github.com:",
        "url.https://github.com/.insteadOf=ssh://git@github.com/",
        "push.default=nothing",
    )
    return run(repo, *args, protocols=protocols, config=config, timeout=timeout, env={"HOME": "/tmp"}).stdout.strip()


def gh(*args: str, input_text: str | None = None, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    binary = os.environ.get("HO_GH_BIN", "gh")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_SPINNER_DISABLED": "1",
        "NO_COLOR": "1",
    }
    for key in ("GH_CONFIG_DIR", "HO_TEST_GH_STATE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    try:
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=_GH_TIMEOUT, env=env,
                                input=input_text, check=False)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"gh {args[0]} timed out") from exc
    if result.returncode not in ok_codes:
        raise GitError(result.stderr.strip()[:500] or f"gh {args[0]} failed")
    return result


def auth_status() -> dict[str, Any]:
    result = gh("auth", "status", "--hostname", "github.com", ok_codes=(0, 1))
    return {"logged_in": result.returncode == 0, "detail": (result.stdout + result.stderr).strip()[:500]}


# ------------------------------------------------------------------------ push


def remote_branch_sha(repo: Path, branch: str) -> str | None:
    out = _git_remote(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return out.split()[0] if out else None


def push(repo: Path, *, ref: str, branch: str, prefix: str, protected: list[str], expected_remote_sha: str | None) -> dict[str, Any]:
    """Push a platform ref to a platform branch. `expected_remote_sha` is the commit the platform
    pushed last (None for a new branch); anything else on the remote refuses the push."""
    check_task_branch(branch, prefix=prefix, protected=protected)
    _github(repo)
    sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    lease = expected_remote_sha or ""
    if lease and not valid_sha(lease):
        raise Refused("invalid expected remote commit")
    _git_remote(repo, "push", "--porcelain", "--no-verify", f"--force-with-lease=refs/heads/{branch}:{lease}",
                "origin", f"{sha}:refs/heads/{branch}")
    return {"branch": branch, "sha": sha}


def delete_branch(repo: Path, *, branch: str, prefix: str, protected: list[str]) -> dict[str, Any]:
    check_task_branch(branch, prefix=prefix, protected=protected)
    _github(repo)
    _git_remote(repo, "push", "--porcelain", "--no-verify", "origin", f":refs/heads/{branch}")
    return {"branch": branch, "deleted": True}


# ----------------------------------------------------------------- pull requests

_PR_FIELDS = "number,url,state,headRefName,headRefOid,baseRefName,isDraft,mergeable,mergeStateStatus,mergeCommit"


def pr_view(repo: Path, number: int) -> dict[str, Any]:
    info = _github(repo)
    return json.loads(gh("pr", "view", str(number), "--repo", info["repo"], "--json", _PR_FIELDS).stdout)


def create_or_update_pr(repo: Path, *, branch: str, base: str, title: str, body: str, prefix: str,
                        protected: list[str]) -> dict[str, Any]:
    check_task_branch(branch, prefix=prefix, protected=protected)
    info = _github(repo)
    existing = json.loads(gh("pr", "list", "--repo", info["repo"], "--head", branch, "--state", "open",
                             "--json", "number").stdout or "[]")
    if existing:
        number = int(existing[0]["number"])
        gh("pr", "edit", str(number), "--repo", info["repo"], "--title", title[:250], "--body-file", "-", input_text=body)
        created = False
    else:
        gh("pr", "create", "--repo", info["repo"], "--base", base, "--head", branch, "--title", title[:250],
           "--body-file", "-", input_text=body)
        number = int(json.loads(gh("pr", "list", "--repo", info["repo"], "--head", branch, "--state", "open",
                                   "--json", "number").stdout)[0]["number"])
        created = True
    return {"created": created, **pr_view(repo, number)}


def pr_checks(repo: Path, number: int) -> dict[str, Any]:
    info = _github(repo)
    # Exit code 8 means checks are still pending; 1 can mean failing checks or none configured.
    result = gh("pr", "checks", str(number), "--repo", info["repo"], "--json", "name,state,bucket,link", ok_codes=(0, 1, 8))
    try:
        checks = json.loads(result.stdout or "[]")
    except ValueError:
        checks = []
    buckets = {c.get("bucket") for c in checks}
    summary = ("NONE" if not checks else "FAIL" if "fail" in buckets else "PENDING" if "pending" in buckets
               else "PASS")
    return {"number": number, "summary": summary, "checks": checks}


def merge_pr(repo: Path, *, approval_id: str, number: int, target_branch: str, target_sha: str, head_sha: str,
             method: str) -> dict[str, Any]:
    """Approved merge of a pull request (section 41). The PR head, its base branch, and the
    base branch's commit must all still be the approved ones."""
    record = f"refs/hermes/merges/{approval_id}"
    done = try_git(repo, "rev-parse", "--verify", "--quiet", record)
    if done:
        return {"merge_sha": done, "target_branch": target_branch, "method": method, "already_merged": True, "pr": number}
    info = _github(repo)
    pr = pr_view(repo, number)
    if pr["state"] == "MERGED":
        raise Refused("the pull request was merged outside the platform")
    if pr["headRefOid"] != head_sha or pr["baseRefName"] != target_branch:
        raise Refused("the pull request head or base changed after approval")
    if remote_branch_sha(repo, target_branch) != target_sha:
        raise Refused(f"{target_branch} on GitHub moved after approval")
    flag = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}[method]
    gh("pr", "merge", str(number), "--repo", info["repo"], flag, "--match-head-commit", head_sha)
    merged = pr_view(repo, number)
    if merged["state"] != "MERGED":
        raise GitError("GitHub did not report the pull request as merged (merge queue?)")
    merge_sha = (merged.get("mergeCommit") or {}).get("oid")
    _git_remote(repo, "fetch", "-q", "--no-tags", "origin", f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}")
    tip = git(repo, "rev-parse", f"refs/remotes/origin/{target_branch}")
    # The record points at the merged target, which post-merge verification checks out.
    run(repo, "update-ref", record, tip)
    return {"merge_sha": merge_sha if merge_sha and valid_sha(merge_sha) else tip, "target_branch": target_branch,
            "method": method, "already_merged": False, "pr": number}
