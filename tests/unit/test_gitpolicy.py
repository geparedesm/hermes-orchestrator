from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ho_core.gitpolicy import (
    ChangeLevel,
    FileChange,
    GitPolicyError,
    check_task_branch,
    classify_divergence,
    parse_hunks,
    protected_branches,
    sign_merge,
    verify_merge,
)

KEY = b"k" * 32
SUBJECT = {"project": "app", "target_branch": "main", "target_sha": "a" * 40, "head_sha": "b" * 40, "method": "merge",
           "pr_number": None}


def token(**overrides):
    return sign_merge(KEY, approval_id="ap-1", subject={**SUBJECT, **overrides},
                      expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))


def test_protected_set_always_includes_main_master_and_default():
    assert protected_branches(["release"], "develop") == {"main", "master", "release", "develop"}
    assert protected_branches([], None) == {"main", "master"}


@pytest.mark.parametrize("branch", ["main", "master", "develop", "feature/x", "hermes", "-hermes/x", "hermes/../main", "hermes/x.lock"])
def test_platform_branches_only(branch):
    with pytest.raises(GitPolicyError):
        check_task_branch(branch, prefix="hermes/", protected=protected_branches([], "develop"))
    check_task_branch("hermes/t-1/w1", prefix="hermes/", protected=["main"])


def test_merge_token_round_trip_and_tampering():
    assert verify_merge(KEY, token())["head_sha"] == "b" * 40
    for field, value in (("target_sha", "c" * 40), ("method", "squash"), ("pr_number", 7), ("project", "other")):
        forged = token()
        forged["subject"][field] = value
        with pytest.raises(GitPolicyError, match="signature"):
            verify_merge(KEY, forged)
    with pytest.raises(GitPolicyError, match="signature"):
        verify_merge(b"other-key", token())
    with pytest.raises(GitPolicyError, match="expired"):
        verify_merge(KEY, sign_merge(KEY, approval_id="x", subject=SUBJECT, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    with pytest.raises(GitPolicyError, match="exact commits"):
        verify_merge(KEY, token(target_sha="main"))
    with pytest.raises(GitPolicyError):
        verify_merge(b"", token())


DIFF = """diff --git a/app.py b/app.py
index 1..2 100644
--- a/app.py
+++ b/app.py
@@ -5 +5 @@ x
-line 5
+changed
@@ -20,0 +21,2 @@
+added
+added
diff --git a/b.txt b/b.txt
--- a/b.txt
+++ b/b.txt
@@ -1,3 +1 @@
-a
-b
-c
+z
"""


def test_parse_hunks_in_base_coordinates():
    assert parse_hunks(DIFF) == {"app.py": [(5, 5), (20, 21)], "b.txt": [(1, 3)]}


def test_classification():
    task = {"app.py": FileChange("app.py", "M", [(5, 5)]), "new.py": FileChange("new.py", "A")}
    assert classify_divergence({}, task)[0] == ChangeLevel.NONE
    assert classify_divergence({"README.md": FileChange("README.md", "M", [(1, 1)])}, task)[0] == ChangeLevel.LOW
    assert classify_divergence({"app.py": FileChange("app.py", "M", [(30, 31)])}, task)[0] == ChangeLevel.MEDIUM
    assert classify_divergence({"app.py": FileChange("app.py", "M", [(7, 7)])}, task)[0] == ChangeLevel.HIGH  # within 3 lines
    assert classify_divergence({"app.py": FileChange("app.py", "D")}, task)[0] == ChangeLevel.CRITICAL
    assert classify_divergence({"new.py": FileChange("new.py", "A")}, task)[0] == ChangeLevel.HIGH
    level, files = classify_divergence({"app.py": FileChange("app.py", "M", [(30, 31)])}, task, sensitive_paths=["*.py"])
    assert level == ChangeLevel.HIGH and "sensitive" in files[0].reason
    assert classify_divergence({"app.py": FileChange("app.py", "M", [(5, 5)])}, task, critical_paths=["app.py"])[0] == ChangeLevel.CRITICAL
