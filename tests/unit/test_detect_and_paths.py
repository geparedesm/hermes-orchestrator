import json
from pathlib import Path

import pytest

from ho_core import schemas
from ho_core.detect import detect, propose_project_config
from ho_core.paths import PathOutsideRoot, host_to_relative, resolve_inside, slugify


def write(root: Path, rel: str, content: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_node_project_detection(tmp_path):
    write(tmp_path, "package.json", json.dumps({
        "scripts": {"build": "tsc", "test": "vitest", "lint": "eslint ."},
        "dependencies": {"next": "15", "react": "19"},
        "devDependencies": {"typescript": "5", "@playwright/test": "1"},
    }))
    write(tmp_path, "package-lock.json", "{}")
    write(tmp_path, "src/auth/login.ts", "")
    write(tmp_path, "src/app.test.ts", "")
    write(tmp_path, "Dockerfile", "FROM node")
    write(tmp_path, "compose.yaml", "services: {}")
    write(tmp_path, ".github/workflows/ci.yml", "")
    write(tmp_path, "README.md", "# demo")
    write(tmp_path, "docs/adr/0001-use-next.md", "")
    write(tmp_path, ".hermes/worktrees/task-1/package.json", "{}")

    report = detect(tmp_path)
    assert report.profiles == ["node"]
    assert {"nextjs", "react", "playwright"} <= set(report.frameworks)
    assert report.commands["install"] == "npm ci"
    assert report.commands["test"] == "npm test"
    assert report.commands["typecheck"] == "npx tsc --noEmit"
    assert report.dockerfiles == ["Dockerfile"]
    assert report.compose_files == ["compose.yaml"]
    assert report.ci == [".github/workflows/ci.yml"]
    assert report.adrs == ["docs/adr/0001-use-next.md"]
    assert report.has_tests
    assert any("authentication" in r for r in report.risks)
    # Task clones under .hermes/worktrees are not project content.
    assert not any("worktrees" in f for f in report.dependency_manifests)

    proposal = propose_project_config("demo", report)
    schemas.validate("project", proposal)
    assert proposal["quality_gate"]["browser_tests"] is True


def test_python_project_without_tests(tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nname="x"\ndependencies=["fastapi"]\n[tool.ruff]\n')
    report = detect(tmp_path)
    assert report.profiles == ["python"]
    assert report.commands.get("lint") == "ruff check ."
    assert not report.has_tests
    assert any("No tests" in r for r in report.risks)
    schemas.validate("project", propose_project_config("x", report))


def test_empty_project_is_generic(tmp_path):
    report = detect(tmp_path)
    assert report.profiles == ["generic"]
    schemas.validate("project", propose_project_config("empty", report))


def test_symlink_outside_root_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package.json").write_text("{}")
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").symlink_to(outside / "package.json")
    report = detect(project)
    assert "install" not in report.commands


def test_host_to_relative():
    assert host_to_relative("/Users/me/HermesProjects", "/Users/me/HermesProjects/app") == "app"
    assert host_to_relative("/Users/me/HermesProjects", "app") == "app"
    for bad in ("/Users/me/Documents/app", "/Users/me/HermesProjects/../Documents", "/Users/me/HermesProjects", "../x"):
        with pytest.raises(PathOutsideRoot):
            host_to_relative("/Users/me/HermesProjects", bad)


def test_resolve_inside_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    assert resolve_inside(root, "a/b") == (root / "a/b").resolve()
    with pytest.raises(PathOutsideRoot):
        resolve_inside(root, "link")
    with pytest.raises(PathOutsideRoot):
        resolve_inside(root, "../x")


def test_slugify():
    assert slugify("My App!") == "my-app"
    with pytest.raises(ValueError):
        slugify("!!!")


def test_standard_library_python_project_gets_unittest(tmp_path):
    write(tmp_path, "shop/__init__.py", "x = 1\n")
    write(tmp_path, "tests/test_shop.py", "import unittest\n")
    report = detect(tmp_path)
    assert "python" in report.profiles and report.commands["test"] == "python -m unittest discover -s tests -v"
    proposal = propose_project_config("shop", report)
    assert proposal["commands"]["test"] == "python -m unittest discover -s tests -v" and proposal["quality_gate"]["tests"] is True


def test_unittest_packages_and_top_level_tests_use_default_discovery(tmp_path):
    write(tmp_path, "app.py", "x = 1\n")
    write(tmp_path, "tests/__init__.py", "")
    write(tmp_path, "tests/test_app.py", "import unittest\n")
    assert detect(tmp_path).commands["test"] == "python -m unittest -v"
