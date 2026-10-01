"""Hard invariants and container planning (SECURITY_MODEL.md section 8.1).

`build_plan` turns an execution request into a complete container plan or
raises `Rejected`. Agent Manager constructs every mount, network, and security
setting itself from the capability grant; callers cannot pass raw Docker
options, so forbidden mounts (the Docker socket, host paths outside the
assigned workspace, other projects) cannot even be expressed.

This module is pure (no Docker calls) so every invariant is unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import stack
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ho_core import schemas
from ho_core.paths import PathOutsideRoot, resolve_inside

WORKER_UID = 10001
PROXY_PORT = 3128
AGENT_ROLES = {"ORCHESTRATOR", "DEVELOPER", "REVIEWER"}
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# Environment variables that could redirect tools to daemons or credentials.
_ENV_DENYLIST = {
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "CONTAINER_HOST", "KUBECONFIG",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GIT_ASKPASS", "SSH_AUTH_SOCK",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
}
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_INPUT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_INPUT_BYTES = 512 * 1024
PROVIDERS = ("claude", "codex")
# Where each provider's credential volume is mounted, and whether the runner may write
# to it: Codex refreshes its ChatGPT login during use; Claude's setup-token does not refresh.
CREDENTIAL_MOUNT = "/run/ho-credentials"
CREDENTIAL_WRITABLE = {"claude": False, "codex": True}
SESSION_MOUNT = "/run/ho-sessions"
CACHE_MOUNT = "/cache"
# Toolchain profile -> (cache name, variable pointing the package manager at it) (MASTER_SPEC section 71).
# Download caches only: node_modules, virtualenvs, and build outputs stay in the workspace.
CACHE_ECOSYSTEMS = {
    "python": ("pip", "PIP_CACHE_DIR"),
    "node": ("npm", "npm_config_cache"),
    "java": ("gradle", "GRADLE_USER_HOME"),
    "flutter": ("pub", "PUB_CACHE"),
    "php": ("composer", "COMPOSER_CACHE_DIR"),
}
INPUT_MOUNT = "/run/ho-input"
SECRETS_MOUNT = "/run/ho/secrets"


class Rejected(ValueError):
    """The request violates a hard invariant."""


@dataclass(frozen=True)
class Mount:
    kind: str  # "bind" or "volume"
    source: str  # host path (bind) or volume name
    target: str
    read_only: bool


@dataclass
class ContainerPlan:
    execution: str
    short: str
    task: str
    project: str
    role: str
    image: str  # symbolic name, resolved by images.py
    command: list[str]
    env: dict[str, str]
    labels: dict[str, str]
    mounts: list[Mount]
    cpus: float
    memory_bytes: int
    pids_limit: int
    expires_at: datetime
    egress: str  # NONE | PROVIDER_ONLY | ALLOWLIST | STANDARD
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    test_services: bool = False
    credential_volume: str | None = None
    provider: str | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    # Secret reference -> (NAME, delivery "file" or "env"); values are read only when the container starts.
    secrets: dict[str, tuple[str, str]] = field(default_factory=dict)
    session_volume: str | None = None
    caches: list[tuple[str, str]] = field(default_factory=list)  # (volume, ecosystem)

    @property
    def worker_name(self) -> str:
        return f"ho-w-{self.short}"

    @property
    def proxy_name(self) -> str:
        return f"ho-p-{self.short}"

    @property
    def execution_network(self) -> str:
        return f"ho-e-{self.short}"

    @property
    def service_network(self) -> str:
        return f"ho-t-{stack.task_slug(self.task)}-svc"

    @property
    def output_volume(self) -> str:
        return f"ho-out-{self.short}"

    @property
    def input_volume(self) -> str:
        return f"ho-in-{self.short}"


def resolve_workspace(project_path: str, workspace: str, projects_root: Path) -> tuple[PurePosixPath, Path]:
    """Validate `<project>/.hermes/worktrees/<name>` and return (relative path, resolved path)."""
    project = PurePosixPath(project_path)
    relative = PurePosixPath(workspace)
    # Only <project>/.hermes/worktrees/<name> may be mounted (ARCHITECTURE.md section 8).
    if relative.parent != project / ".hermes" / "worktrees" or not relative.name or relative.name.startswith("."):
        raise Rejected(f"workspace must be {project}/.hermes/worktrees/<name>")
    try:
        resolved = resolve_inside(projects_root, str(relative))
    except PathOutsideRoot as exc:
        raise Rejected(str(exc)) from exc
    # A symlink anywhere below the projects root could redirect the bind mount on the host.
    components = [projects_root.joinpath(*relative.parts[: i + 1]) for i in range(len(relative.parts))]
    if any(c.is_symlink() for c in components):
        raise Rejected("workspace path must not contain symbolic links")
    if not resolved.is_dir():
        raise Rejected(f"workspace {relative} does not exist")
    return relative, resolved


def _workspace_mount(request: dict[str, Any], projects_root: Path, projects_root_host: str, access: str) -> Mount:
    workspace = request.get("workspace")
    if not workspace:
        raise Rejected("grant includes workspace access but no workspace was given")
    relative, _ = resolve_workspace(request["project_path"], workspace, projects_root)
    host = f"{projects_root_host.rstrip('/')}/{relative}"
    return Mount("bind", host, "/workspace", read_only=(access == "READ"))


def _project_read_mounts(request: dict[str, Any], projects_root: Path, projects_root_host: str, allowed: list[str]) -> list[Mount]:
    mounts = []
    for item in request.get("project_read", []):
        if item["slug"] not in allowed:
            raise Rejected(f"project {item['slug']} is not in the grant's project_read")
        relative = PurePosixPath(item["path"])
        try:
            resolved = resolve_inside(projects_root, str(relative))
        except PathOutsideRoot as exc:
            raise Rejected(str(exc)) from exc
        if resolved == projects_root.resolve() or len(relative.parts) != 1 or not (resolved / ".git").exists():
            raise Rejected(f"{relative} is not a project repository directly inside the projects root")
        mounts.append(Mount("bind", f"{projects_root_host.rstrip('/')}/{relative}", f"/projects/{item['slug']}", read_only=True))
    if set(allowed) - {i["slug"] for i in request.get("project_read", [])}:
        raise Rejected("every project in the grant's project_read needs a path")
    return mounts


def build_plan(
    request: dict[str, Any],
    *,
    platform: dict[str, Any],
    projects_root: Path,
    projects_root_host: str,
    now: datetime | None = None,
) -> ContainerPlan:
    now = now or datetime.now(timezone.utc)
    grant = request.get("grant") or {}
    errors = schemas.errors_for("capability", grant)
    if errors:
        raise Rejected("invalid capability grant: " + "; ".join(errors[:5]))

    execution = request.get("execution_id", "")
    if not _ID.match(execution) or grant["execution"] != execution:
        raise Rejected("execution_id must be a UUID matching the grant")
    for key in ("task", "project", "role"):
        if request.get(key) != grant[key]:
            raise Rejected(f"{key} does not match the grant")
    caps = grant["capabilities"]
    if caps["docker"] != "NONE":  # also enforced by the schema; kept as an explicit invariant
        raise Rejected("executions never receive Docker access")
    expires_at = datetime.fromisoformat(grant["expires_at"])
    if expires_at <= now:
        raise Rejected("capability grant has expired")
    if grant.get("revoked_at"):
        raise Rejected("capability grant was revoked")
    secrets: dict[str, tuple[str, str]] = {}
    env_delivery = set(request.get("secret_env") or [])
    if not env_delivery <= set(caps["secrets"]):
        raise Rejected("secret_env may only name secrets in the grant")
    for ref in caps["secrets"]:
        project, _environment, name = ref.split("/")
        if project != grant["project"]:
            raise Rejected(f"secret {ref} belongs to another project")
        secrets[ref] = (name, "env" if ref in env_delivery else "file")

    command = request.get("command") or []
    if not isinstance(command, list) or not command or not all(isinstance(c, str) and len(c) < 8192 for c in command):
        raise Rejected("command must be a non-empty list of strings")

    env: dict[str, str] = {}
    secret_names = {name for name, _ in secrets.values()}
    for key, value in (request.get("env") or {}).items():
        if not _ENV_KEY.match(key) or key in _ENV_DENYLIST or key.startswith("HO_") or key in secret_names:
            raise Rejected(f"environment variable {key} is not allowed")
        if not isinstance(value, str) or len(value) > 4096:
            raise Rejected(f"environment variable {key} must be a short string")
        env[key] = value

    mounts: list[Mount] = []
    if caps["workspace"] != "NONE":
        mounts.append(_workspace_mount(request, projects_root, projects_root_host, caps["workspace"]))
    elif request.get("workspace"):
        raise Rejected("the grant does not allow workspace access")
    mounts += _project_read_mounts(request, projects_root, projects_root_host, caps.get("project_read", []))

    image = request.get("image", "")
    credential_volume = provider = None
    if grant["provider_credential"]:
        provider = grant["provider_credential"]["provider"]
        credential_volume = f"cred-{provider}-{grant['provider_credential']['identity']}"
        mounts.append(Mount("volume", credential_volume, f"{CREDENTIAL_MOUNT}/{provider}",
                            read_only=not CREDENTIAL_WRITABLE[provider]))
    # Provider images run a provider CLI; they are only for executions holding that provider's credential.
    image_provider = next((p for p in PROVIDERS if image.startswith(f"{p}-")), None)
    if image_provider != provider:
        raise Rejected(f"image {image!r} does not match the grant's provider ({provider or 'none'})")

    session_volume = None
    if request.get("session"):
        if provider is None:
            raise Rejected("only provider executions keep a session store")
        session_volume = f"ho-sess-{stack.task_slug(grant['task'])}-{provider}"
        mounts.append(Mount("volume", session_volume, SESSION_MOUNT, read_only=False))

    # Dependency caches: per project and ecosystem, for executions that install packages into a workspace.
    caches: list[tuple[str, str]] = []
    cache_config = platform["machine"].get("dependency_cache") or {}
    if caps["workspace"] == "WRITE" and cache_config.get("enabled", True):
        toolchains = image.split("-", 1)[1].split("-") if "-" in image else []
        for profile, (ecosystem, variable) in CACHE_ECOSYSTEMS.items():
            if profile in toolchains:
                volume = f"ho-cache-{grant['project']}-{ecosystem}"
                mounts.append(Mount("volume", volume, f"{CACHE_MOUNT}/{ecosystem}", read_only=False))
                env[variable] = f"{CACHE_MOUNT}/{ecosystem}"
                caches.append((volume, ecosystem))

    inputs: dict[str, str] = {}
    for name, content in (request.get("inputs") or {}).items():
        if not _INPUT_NAME.match(str(name)) or not isinstance(content, str):
            raise Rejected(f"invalid input file {name!r}")
        inputs[name] = content
    if sum(len(c.encode()) for c in inputs.values()) > MAX_INPUT_BYTES:
        raise Rejected(f"input files exceed {MAX_INPUT_BYTES // 1024} KiB")

    short = execution.replace("-", "")[-12:]
    mounts.append(Mount("volume", f"ho-out-{short}", "/output", read_only=False))

    profile = grant["resources"]["profile"]
    # Runners use the machine's runner limits; agent workers their resource profile (section 34).
    runner_limits = {"TESTER": "test", "BROWSER": "browser"}.get(grant["role"])
    limits = (platform["machine"]["runners"][runner_limits] if runner_limits
              else platform["machine"]["resource_profiles"][profile])
    network = caps["network"]
    domains = sorted(set(network.get("allowed_domains", [])))
    if network["egress"] in ("PROVIDER_ONLY", "ALLOWLIST") and grant["provider_credential"]:
        provider = grant["provider_credential"]["provider"]
        domains = sorted(set(domains) | set(platform["machine"].get("provider_domains", {}).get(provider, [])))

    labels = {
        "ho.managed": "true",
        "ho.stack": stack.NAME,
        "ho.kind": "worker",
        "ho.execution": execution,
        "ho.task": grant["task"],
        "ho.project": grant["project"],
        "ho.role": grant["role"],
        "ho.epoch": str(grant["lease_epoch"]),
        "ho.grant": grant["grant_id"],
        "ho.expires_at": expires_at.isoformat(),
        # References only (never values): used to redact delivered values from collected output.
        "ho.secrets": ",".join(sorted(secrets)),
    }
    return ContainerPlan(
        execution=execution,
        short=short,
        task=grant["task"],
        project=grant["project"],
        role=grant["role"],
        image=image,
        command=command,
        env=env,
        labels=labels,
        mounts=mounts,
        cpus=float(limits["cpus"]),
        memory_bytes=int(float(limits["memory_gb"]) * 1024**3),
        pids_limit=512,
        expires_at=expires_at,
        egress=network["egress"],
        allowed_domains=domains,
        denied_domains=sorted(set(request.get("denied_domains") or [])),
        test_services=bool(network["test_services"]),
        credential_volume=credential_volume,
        provider=provider,
        inputs=inputs,
        secrets=secrets,
        session_volume=session_volume,
        caches=caches,
    )
