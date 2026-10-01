"""Read-only project environment detection for onboarding and drift checks (MASTER_SPEC sections 15, 17).

Runs inside git-service against a read-only mount. It only reads small files,
never executes repository content, and never follows symlinks out of the root.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 256 * 1024
MAX_WALK_ENTRIES = 20000
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".dart_tool", "vendor", "target", ".gradle"}


@dataclass
class DetectionReport:
    profiles: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    dockerfiles: list[str] = field(default_factory=list)
    compose_files: list[str] = field(default_factory=list)
    ci: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    adrs: list[str] = field(default_factory=list)
    dependency_manifests: list[str] = field(default_factory=list)
    sensitive_paths: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    has_tests: bool = False
    existing_project_yaml: bool = False
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_text(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _walk(root: Path) -> tuple[list[str], bool]:
    """Relative file paths (depth-limited, symlinks not followed)."""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        # Task clones and generated overrides are platform output, not project content.
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not (rel_dir == ".hermes" and d in {"worktrees", "generated"})
        ]
        if depth >= 6:
            dirnames[:] = []
        for name in filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                continue  # never read through links; they may point outside the project
            files.append(name if rel_dir == "." else f"{rel_dir}/{name}".replace(os.sep, "/"))
            if len(files) >= MAX_WALK_ENTRIES:
                return files, True
    return files, False


def detect(root: Path) -> DetectionReport:
    report = DetectionReport()
    files, report.truncated = _walk(root)
    names = set(files)
    top = {f for f in files if "/" not in f}

    def add(lst: list[str], value: str) -> None:
        if value not in lst:
            lst.append(value)

    # Node
    if "package.json" in top:
        add(report.profiles, "node")
        add(report.languages, "javascript")
        add(report.dependency_manifests, "package.json")
        text = _read_text(root, "package.json")
        pkg: dict[str, Any] = {}
        if text:
            try:
                pkg = json.loads(text)
            except json.JSONDecodeError:
                add(report.risks, "package.json is not valid JSON")
        scripts = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}
        runner = "pnpm" if "pnpm-lock.yaml" in top else "yarn" if "yarn.lock" in top else "npm"
        report.commands["install"] = {"npm": "npm ci" if "package-lock.json" in top else "npm install",
                                      "pnpm": "pnpm install --frozen-lockfile", "yarn": "yarn install --frozen-lockfile"}[runner]
        for key, script in (("build", "build"), ("test", "test"), ("lint", "lint"), ("typecheck", "typecheck"), ("e2e", "e2e")):
            if script in scripts:
                report.commands[key] = f"{runner} run {script}" if script != "test" else f"{runner} test"
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} if isinstance(pkg, dict) else {}
        for dep, framework in (("next", "nextjs"), ("react", "react"), ("vue", "vue"), ("@angular/core", "angular"),
                               ("express", "express"), ("@nestjs/core", "nestjs"), ("svelte", "svelte")):
            if dep in deps:
                add(report.frameworks, framework)
        if "typescript" in deps or "tsconfig.json" in top:
            add(report.languages, "typescript")
            report.commands.setdefault("typecheck", "npx tsc --noEmit")
        if "@playwright/test" in deps:
            add(report.frameworks, "playwright")

    # Python
    py_manifests = [m for m in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile") if m in top]
    if py_manifests:
        add(report.profiles, "python")
        add(report.languages, "python")
        report.dependency_manifests.extend(m for m in py_manifests if m not in report.dependency_manifests)
        text = _read_text(root, "pyproject.toml") or ""
        tools: dict[str, Any] = {}
        if text:
            try:
                tools = tomllib.loads(text).get("tool", {})
            except tomllib.TOMLDecodeError:
                add(report.risks, "pyproject.toml is not valid TOML")
        if "pytest" in tools or any(re.match(r"(tests?/.*|.*/)?test_[^/]+\.py$", f) for f in files):
            report.commands.setdefault("test", "pytest")
        if "ruff" in tools:
            report.commands.setdefault("lint", "ruff check .")
        if "mypy" in tools:
            report.commands.setdefault("typecheck", "mypy .")
        for dep, framework in (("django", "django"), ("fastapi", "fastapi"), ("flask", "flask")):
            if re.search(rf"(?im)^\s*[\"']?{dep}\b", text + (_read_text(root, "requirements.txt") or "")):
                add(report.frameworks, framework)

    elif any(f.endswith(".py") for f in files):
        # Python without a dependency manifest (standard library only): the standard test runner always exists.
        add(report.profiles, "python")
        add(report.languages, "python")
        tests = [f for f in files if re.match(r"(tests?/.*|.*/)?test_[^/]+\.py$", f)]
        if tests:
            report.commands.setdefault("test", _unittest_command(tests, files))

    # Flutter / Dart
    if "pubspec.yaml" in top:
        add(report.profiles, "flutter")
        add(report.languages, "dart")
        add(report.dependency_manifests, "pubspec.yaml")
        report.commands.setdefault("install", "flutter pub get")
        report.commands.setdefault("test", "flutter test")
        report.commands.setdefault("lint", "flutter analyze")

    # PHP
    if "composer.json" in top:
        add(report.profiles, "php")
        add(report.languages, "php")
        add(report.dependency_manifests, "composer.json")
        report.commands.setdefault("install", "composer install --no-interaction")
        if "phpunit.xml" in top or "phpunit.xml.dist" in top:
            report.commands.setdefault("test", "vendor/bin/phpunit")
        if "artisan" in top:
            add(report.frameworks, "laravel")

    # Java
    if "pom.xml" in top:
        add(report.profiles, "java")
        add(report.languages, "java")
        add(report.dependency_manifests, "pom.xml")
        report.commands.setdefault("build", "mvn -B package -DskipTests")
        report.commands.setdefault("test", "mvn -B test")
    elif {"build.gradle", "build.gradle.kts"} & top:
        add(report.profiles, "java")
        add(report.languages, "java")
        report.dependency_manifests.extend(sorted({"build.gradle", "build.gradle.kts"} & top))
        gradle = "./gradlew" if "gradlew" in top else "gradle"
        report.commands.setdefault("build", f"{gradle} build -x test")
        report.commands.setdefault("test", f"{gradle} test")

    if not report.profiles:
        report.profiles.append("generic")

    # Containers, CI, docs
    report.dockerfiles = sorted(f for f in files if re.search(r"(^|/)(Dockerfile(\.[\w.-]+)?|[\w.-]+\.Dockerfile)$", f))
    report.compose_files = sorted(f for f in files if re.search(r"(^|/)(docker-)?compose(\.[\w-]+)?\.ya?ml$", f) and "/" not in f)
    report.ci = sorted(f for f in files if f.startswith(".github/workflows/") or f in {".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml", "azure-pipelines.yml", "bitbucket-pipelines.yml"})
    for doc in ("README.md", "README", "README.rst", "CLAUDE.md", "AGENTS.md", "ARCHITECTURE.md", "CONTRIBUTING.md", "SECURITY.md"):
        if doc in top:
            report.docs.append(doc)
    report.adrs = sorted(f for f in files if re.search(r"(^|/)(adr|adrs|decisions)/[^/]+\.md$", f, re.IGNORECASE))
    report.existing_project_yaml = ".hermes/project.yaml" in names

    report.has_tests = any(re.search(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|(\.|_)(test|spec)\.\w+$|^test_.*\.py$|/test_[^/]+\.py$", f) for f in files)

    # Sensitive areas (MASTER_SPEC section 57)
    patterns = {
        "authentication": r"(^|/)(auth|authentication|login|oauth|sso)(/|\.)",
        "payments": r"(^|/)(payment|payments|billing|checkout|stripe)(/|\.)",
        "migrations": r"(^|/)(migrations?|db/migrate|alembic)/",
        "infrastructure": r"(^|/)(terraform|infra|infrastructure|k8s|kubernetes|helm|deploy)(/|$)",
    }
    for label, pattern in patterns.items():
        matches = sorted({f.split("/")[0] if "/" in f else f for f in files if re.search(pattern, f, re.IGNORECASE)})
        for match in matches[:5]:
            add(report.sensitive_paths, f"{match}/**" if not match.endswith((".py", ".ts", ".js")) else match)
        if matches:
            add(report.risks, f"Sensitive area detected: {label}")
    if report.dockerfiles:
        add(report.sensitive_paths, "**/Dockerfile*")
    if report.ci:
        add(report.sensitive_paths, ".github/workflows/**")

    # Risks
    if not report.has_tests:
        add(report.risks, "No tests detected; the Test Gap Policy will require alternative verification")
    committed_env = [f for f in files if re.search(r"(^|/)\.env(\.[\w-]+)?$", f) and not f.endswith((".example", ".sample", ".template"))]
    if committed_env:
        add(report.risks, f"Environment files present in the working tree: {committed_env[:5]}")
    if report.truncated:
        add(report.risks, f"Scan truncated at {MAX_WALK_ENTRIES} files")
    return report


def propose_project_config(name: str, report: DetectionReport | dict[str, Any]) -> dict[str, Any]:
    """Build a .hermes/project.yaml proposal from a detection report."""
    data = report.to_dict() if isinstance(report, DetectionReport) else report
    commands = {k: v for k, v in data.get("commands", {}).items() if k in {"install", "build", "test", "lint", "typecheck", "e2e"}}
    has = commands.__contains__
    proposal: dict[str, Any] = {
        "version": 1,
        "project": {"name": name},
        "autonomy": "BALANCED",
        "toolchain": {"profiles": data.get("profiles") or ["generic"]},
        "agents": {"allowed_providers": ["claude", "codex"], "preferred": {"planning": "claude", "implementation": "codex"}},
        "network": {"development": "standard", "testing": "isolated"},
        "quality_gate": {
            "tests": has("test"),
            "full_suite": has("test"),
            "build": has("build"),
            "lint": has("lint"),
            "typecheck": has("typecheck"),
            "browser_tests": "playwright" in data.get("frameworks", []),
            "ci_checks": bool(data.get("ci")),
        },
        "budget": {"profile": "NORMAL"},
        "git": {"protected_branches": ["main"]},
    }
    if commands:
        proposal["commands"] = commands
    if data.get("sensitive_paths"):
        proposal["verification"] = {"sensitive_paths": data["sensitive_paths"]}
    if data.get("compose_files"):
        proposal["test_environment"] = {"compose_files": data["compose_files"][:3]}
    if "playwright" in data.get("frameworks", []):
        proposal["browser_tests"] = {"enabled": True}
    return proposal


def _unittest_command(tests: list[str], files: list[str]) -> str:
    """unittest's default discovery only enters packages; a plain test directory needs `-s` (Python 3.12 exits 5 when nothing ran)."""
    roots = {t.split("/", 1)[0] if "/" in t else "" for t in tests}
    if len(roots) == 1 and (directory := roots.pop()) and f"{directory}/__init__.py" not in files:
        return f"python -m unittest discover -s {directory} -v"
    return "python -m unittest -v"
