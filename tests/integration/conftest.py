"""Integration fixtures: a fresh migrated database, Redis, a projects root with real
Git repositories, and both services wired in-process.

Requires:
  HO_TEST_DATABASE_URL  superuser URL, e.g. postgresql://ho_test_admin:ho_test_admin@127.0.0.1:55432/postgres
  HO_TEST_REDIS_URL     e.g. redis://127.0.0.1:56379/15
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient

from control_plane.app import Services, build_services, create_app
from control_plane.artifacts import ArtifactStore
from control_plane.auth import Authenticator
from control_plane.context import Context
from control_plane.coordination import Coordinator
from control_plane.db import Database
from control_plane.gitsvc import GitServiceClient
from git_service.app import create_app as create_git_app
from ho_core.config import load_platform_config

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("HO_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("HO_TEST_REDIS_URL")
APP_PASSWORD = "ho_app_test_password"
OPERATOR_TOKEN = "operator-test-token-0123456789"
PLUGIN_TOKEN = "plugin-test-token-0123456789"
GIT_TOKEN = "git-test-token-0123456789"
MERGE_KEY = b"merge-test-key-0123456789"

pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(items):
    if ADMIN_URL and REDIS_URL:
        return
    skip = pytest.mark.skip(reason="set HO_TEST_DATABASE_URL and HO_TEST_REDIS_URL (make test-integration)")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


def _url_for(database: str, user: str | None = None, password: str | None = None) -> str:
    info = psycopg.conninfo.conninfo_to_dict(ADMIN_URL)
    user = user or info["user"]
    password = password or info["password"]
    return f"postgresql://{user}:{password}@{info['host']}:{info.get('port', 5432)}/{database}"


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    name = f"ho_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
        admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN "
            f"CREATE ROLE ho_app LOGIN PASSWORD '{APP_PASSWORD}'; END IF; END $$"
        )
    owner_url = _url_for(name).replace("postgresql://", "postgresql+psycopg://", 1)
    env = {**os.environ, "HO_MIGRATION_DATABASE_URL": owner_url, "HO_MIGRATIONS_DIR": str(ROOT / "migrations")}
    subprocess.run([sys.executable, "-m", "control_plane.migrate"], check=True, env=env, cwd=ROOT)
    with psycopg.connect(_url_for(name), autocommit=True) as conn:
        conn.execute("GRANT USAGE ON SCHEMA public TO ho_app")
    global _OWNER_URL
    _OWNER_URL = _url_for(name)
    yield _url_for(name, "ho_app", APP_PASSWORD)
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name} WITH (FORCE)")


def git_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {"README.md": "# demo\n"}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, env=env)  # noqa: E731
    if not (path / ".git").exists():
        run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "commit", "--allow-empty")
    return path


_OWNER_URL = ""


@pytest.fixture(autouse=True)
def clean_database(request):
    """Every integration test starts from empty tables (the schema is migrated once per session)."""
    if "database" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("database")
    with psycopg.connect(_OWNER_URL, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")]
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "HermesProjects"
    root.mkdir()
    return root


@pytest.fixture
def services(database: str, projects_root: Path, tmp_path: Path) -> Iterator[Services]:
    platform = load_platform_config(ROOT / "config", "mac-m2-pro")
    platform["platform"]["projects_root_host"] = str(projects_root)
    git_client = TestClient(create_git_app(projects_root, GIT_TOKEN, MERGE_KEY))
    db = Database(database, max_size=5)
    db.open()
    redis.Redis.from_url(REDIS_URL).flushdb()
    ctx = Context(
        db=db,
        platform=platform,
        coordinator=Coordinator(REDIS_URL),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        git=GitServiceClient("http://git-service", GIT_TOKEN, client=git_client),
        merge_key=MERGE_KEY,
    )
    auth = Authenticator({OPERATOR_TOKEN: "operator", PLUGIN_TOKEN: "hermes-plugin"})
    yield build_services(ctx, auth, run_scheduler=False)
    db.close()


class Api:
    """Small helper around TestClient with auth and idempotency headers."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.counter = 0

    def headers(self, token: str = OPERATOR_TOKEN, principal: str | None = None, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if principal:
            headers["X-HO-Principal"] = principal
        self.counter += 1
        headers["Idempotency-Key"] = key or f"test-key-{uuid.uuid4().hex}"
        return headers

    def post(self, path: str, json: dict | None = None, **kw):
        return self.client.post(path, json=json, headers=self.headers(**kw))

    def get(self, path: str, **kw):
        return self.client.get(path, headers=self.headers(**kw))

    def delete(self, path: str, **kw):
        return self.client.delete(path, headers=self.headers(**kw))


@pytest.fixture
def api(services: Services) -> Iterator[Api]:
    with TestClient(create_app(services)) as client:
        yield Api(client)
