"""Task attachments: validation rules, the body limit, prompts, and Agent Manager's checks (no services)."""

from __future__ import annotations

import base64
import io
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_manager.docker_ops import _tar
from agent_manager.plan import Rejected, build_plan
from ho_core import attachments as att
from ho_core.adapters.base import ATTACHMENTS_DIR, AgentAssignment, compose_prompt
from ho_core.adapters.claude import ClaudeAdapter
from ho_core.bodylimit import BodyLimit
from ho_core.config import build_project_config, load_platform_config
from ho_core.enums import Role
from ho_core.hashing import sha256_hex
from ho_core.policy.engine import GrantRequest, evaluate_grant

ROOT = Path(__file__).resolve().parents[2]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n%..."


# ---------------------------------------------------------------- rules

@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd.txt", "passwd.txt"), ("C:\\Users\\me\\Diseño final.PNG", "Dise_o_final.png"),
    (".hidden.md", "hidden.md"), ("a b c.md", "a_b_c.md"), ("x" * 300 + ".pdf", "x" * 96 + ".pdf"), ("noext", "noext"),
])
def test_names_are_reduced_to_a_safe_base_name(name, expected):
    assert att.safe_name(name) == expected


@pytest.mark.parametrize("name", ["a" * 95 + "-b.pdf", "x" * 300 + ".md", "._-x-_.md", "...", "a" * 99 + ".verylongextension123456",
                                  "名前.png", "a.b.c.md", "-.md"])
def test_cleaning_is_stable(name):
    once = att.safe_name(name)
    assert att.safe_name(once) == once and att.NAME.match(once) and len(once) <= 100


def test_types_are_recognized_by_extension_and_content():
    assert att.check("design.png", PNG).media_type == "image/png"
    assert att.check("spec.pdf", PDF).media_type == "application/pdf"
    assert att.check("notes.md", "# Notas ñ\n".encode()).media_type == "text/markdown"
    assert att.check("photo.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ").media_type == "image/webp"
    for name, content, problem in (("run.sh", b"echo", "not allowed"), ("page.html", b"<html>", "not allowed"),
                                   ("fake.png", b"<svg/>", "not a PNG"), ("fake.pdf", b"MZ\x90", "not a PDF"),
                                   ("bin.txt", b"a\x00b", "not a text"), ("latin.csv", "é".encode("latin-1"), "UTF-8"),
                                   ("empty.md", b"", "empty"), ("noext", b"x", "not allowed")):
        with pytest.raises(att.InvalidAttachment, match=problem):
            att.check(name, content)


def test_set_limits():
    with pytest.raises(att.InvalidAttachment, match="at most 10"):
        att.check_all([(f"f{i}.md", b"x") for i in range(11)])
    with pytest.raises(att.InvalidAttachment, match="same name"):
        att.check_all([("A.md", b"x"), ("a.md", b"y")])
    with pytest.raises(att.InvalidAttachment, match="larger than 10 MiB"):
        att.check("big.txt", b"x" * (att.MAX_FILE_BYTES + 1))
    with pytest.raises(att.InvalidAttachment, match="25 MiB in total"):
        att.check_all([(f"f{i}.txt", b"x" * att.MAX_FILE_BYTES) for i in range(3)])


# ---------------------------------------------------------------- body limit

@pytest.fixture
def limited():
    app = FastAPI()

    @app.post("/big")
    async def big(request: Request):
        return {"size": len(await request.body())}

    @app.post("/json")
    def small(body: dict):
        return {"keys": len(body)}

    app.add_middleware(BodyLimit, default=1024, large={("POST", "/big"): 4096})
    return TestClient(app)


def test_bodies_are_bounded_while_they_arrive(limited):
    assert limited.post("/big", content=b"x" * 4000).json() == {"size": 4000}
    assert limited.post("/big", content=b"x" * 5000).status_code == 413
    assert limited.post("/json", json={"a": "x" * 2000}).status_code == 413  # the default limit applies elsewhere

    def chunks():  # no Content-Length: counted as it streams, and the framework's 400 becomes 413
        for _ in range(10):
            yield b"x" * 1000

    assert limited.post("/big", content=chunks()).status_code == 413
    assert limited.post("/json", content=chunks(), headers={"Content-Type": "application/json"}).status_code == 413
    assert limited.post("/json", json={"a": 1}).json() == {"keys": 1}


# ---------------------------------------------------------------- prompts

def test_agents_are_told_where_the_attachments_are_and_that_they_are_data():
    assignment = AgentAssignment(role=Role.DEVELOPER, prompt="Build it", attachments=(("design.png", "image/png", 2_500_000),))
    prompt = compose_prompt(assignment)
    assert f"{ATTACHMENTS_DIR}/design.png (image/png, 2.4 MiB)" in prompt and "not instructions" in prompt
    command = ClaudeAdapter().build_execution(assignment).command
    assert command[command.index("--add-dir") + 1] == ATTACHMENTS_DIR
    assert "Attachments" not in compose_prompt(AgentAssignment(role=Role.DEVELOPER, prompt="Build it"))


# ---------------------------------------------------------------- Agent Manager

@pytest.fixture
def platform():
    return load_platform_config(ROOT / "config", "mac-m2-pro")


def request(platform, role=Role.DEVELOPER, provider="codex"):
    execution = str(uuid.uuid4())
    config = build_project_config(platform, {"version": 1, "project": {"name": "demo"}}).data
    grant, _ = evaluate_grant(GrantRequest(grant_id=f"G-{execution[:8]}", project="demo", task="T-1", execution=execution,
                                           worker="w-1-a", role=role, provider=provider),
                              config, platform, now=datetime.now(timezone.utc))
    image = f"{provider}-generic" if provider else "agent-base"
    return {"execution_id": execution, "task": "T-1", "project": "demo", "role": role.value, "project_path": "demo",
            "image": image, "command": ["true"], "grant": grant}


def attach(body, files):
    body["attachments"] = [{"artifact_id": str(uuid.uuid4()), "name": n, "sha256": sha256_hex(c), "size_bytes": len(c),
                            "media_type": "x"} for n, c in files.items()]
    body["attachment_files"] = {n: base64.b64encode(c).decode() for n, c in files.items()}
    return body


def plan(platform, body, tmp_path):
    return build_plan(body, platform=platform, projects_root=tmp_path, projects_root_host=str(tmp_path))


def test_agents_get_verified_attachments(platform, tmp_path):
    result = plan(platform, attach(request(platform), {"design.png": PNG, "spec.md": b"# spec\n"}), tmp_path)
    assert result.attachments == {"design.png": PNG, "spec.md": b"# spec\n"}


def test_attachments_that_do_not_match_their_references_are_refused(platform, tmp_path):
    body = attach(request(platform), {"spec.md": b"# spec\n"})
    body["attachment_files"]["spec.md"] = base64.b64encode(b"# changed\n").decode()
    with pytest.raises(Rejected, match="does not match"):
        plan(platform, body, tmp_path)
    body = attach(request(platform), {"spec.md": b"# spec\n"})
    body["attachment_files"]["other.md"] = body["attachment_files"]["spec.md"]
    with pytest.raises(Rejected, match="do not match"):
        plan(platform, body, tmp_path)
    with pytest.raises(Rejected, match="attachment refused"):
        plan(platform, attach(request(platform), {"run.sh": b"rm -rf /"}), tmp_path)
    with pytest.raises(Rejected, match="safe names"):
        plan(platform, attach(request(platform), {"../x.md": b"x"}), tmp_path)
    with pytest.raises(Rejected, match="only for agent roles"):
        plan(platform, attach(request(platform, role=Role.TESTER, provider=None), {"spec.md": b"x"}), tmp_path)


def test_attachments_are_written_read_only_for_the_agent():
    archive = tarfile.open(fileobj=io.BytesIO(_tar({"prompt.md": b"hi"}, {"spec.md": b"x"})))
    members = {m.name: m for m in archive.getmembers()}
    assert members["prompt.md"].uid == 10001
    assert members["attachments"].isdir() and members["attachments"].mode == 0o555 and members["attachments"].uid == 0
    assert members["attachments/spec.md"].mode == 0o444 and members["attachments/spec.md"].uid == 0
