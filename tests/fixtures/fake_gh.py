#!/usr/bin/env python3
"""Stand-in for the GitHub CLI in tests: the subset of `gh` Git Service uses, backed by a
JSON state file ($HO_TEST_GH_STATE) and a local bare repository acting as origin.

Records every invocation so tests can assert that forbidden flags (--admin, --auto) are never used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ["HO_TEST_GH_STATE"])


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"prs": [], "calls": [], "checks": [], "logged_in": True}


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state))


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", load()["origin"], *args], check=True, capture_output=True, text=True).stdout.strip()


def option(args: list[str], name: str, default: str | None = None) -> str | None:
    return args[args.index(name) + 1] if name in args else default


def view(pr: dict) -> dict:
    head = git("rev-parse", f"refs/heads/{pr['headRefName']}") if pr["state"] == "OPEN" else pr["headRefOid"]
    return {**pr, "headRefOid": head, "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}


def main(args: list[str]) -> int:
    state = load()
    state["calls"].append(args)
    save(state)
    if args[:2] == ["auth", "status"]:
        print("Logged in to github.com" if state["logged_in"] else "not logged in", file=sys.stderr)
        return 0 if state["logged_in"] else 1
    if args[0] != "pr":
        print(f"unsupported: {args}", file=sys.stderr)
        return 2
    command, rest = args[1], args[2:]
    prs = state["prs"]
    if command == "list":
        head = option(rest, "--head")
        print(json.dumps([{"number": p["number"]} for p in prs if p["headRefName"] == head and p["state"] == "OPEN"]))
    elif command == "create":
        number = len(prs) + 1
        prs.append({"number": number, "url": f"https://github.com/test/test/pull/{number}", "state": "OPEN",
                    "headRefName": option(rest, "--head"), "baseRefName": option(rest, "--base"),
                    "title": option(rest, "--title"), "body": sys.stdin.read(), "mergeCommit": None})
        save(state)
        print(prs[-1]["url"])
    elif command == "edit":
        pr = prs[int(rest[0]) - 1]
        pr.update(title=option(rest, "--title"), body=sys.stdin.read())
        save(state)
    elif command == "view":
        print(json.dumps(view(prs[int(rest[0]) - 1])))
    elif command == "checks":
        print(json.dumps(state["checks"]))
        buckets = {c["bucket"] for c in state["checks"]}
        return 8 if "pending" in buckets else 1 if "fail" in buckets else 0
    elif command == "merge":
        pr = prs[int(rest[0]) - 1]
        head = git("rev-parse", f"refs/heads/{pr['headRefName']}")
        if option(rest, "--match-head-commit") != head:
            print("head commit does not match", file=sys.stderr)
            return 1
        base = git("rev-parse", f"refs/heads/{pr['baseRefName']}")
        tree = git("merge-tree", "--write-tree", base, head).splitlines()[0]
        commit = git("-c", "user.name=GitHub", "-c", "user.email=noreply@github.com", "commit-tree", tree,
                     "-p", base, "-p", head, "-m", f"Merge pull request #{pr['number']}")
        git("update-ref", f"refs/heads/{pr['baseRefName']}", commit, base)
        pr.update(state="MERGED", headRefOid=head, mergeCommit={"oid": commit})
        save(state)
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
