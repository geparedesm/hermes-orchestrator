"""Phase 11 against the real Docker daemon: dependency caches (MASTER_SPEC section 71)."""

from __future__ import annotations

import pytest

from ho_core.enums import Role

pytestmark = pytest.mark.docker


def cached_runner(api, task, command, project="proj-a"):
    body = api.request(Role.TESTER, command, task=task, provider=None, workspace="WRITE", egress="NONE", project=project)
    body["image"] = "runner-python"  # a python toolchain: the pip cache is mounted
    if project != "proj-a":
        (api.projects_root / project / ".hermes" / "worktrees" / "w1").mkdir(parents=True, exist_ok=True)
        body["workspace"] = f"{project}/.hermes/worktrees/w1"
    return body


def test_cache_is_writable_kept_per_project_and_trimmed(api, ops, client):
    for volume in ("ho-cache-proj-a-pip", "ho-cache-proj-b-pip"):
        for container in client.containers.list(all=True, filters={"volume": volume}):
            container.remove(force=True)  # left over by an interrupted earlier run
        try:
            client.volumes.get(volume).remove(force=True)
        except Exception:  # noqa: BLE001 - not there yet
            pass
    first = api.run(cached_runner(api, "T-970001", 'echo "$PIP_CACHE_DIR"; id -u; head -c 3000000 /dev/urandom > "$PIP_CACHE_DIR/old.whl"; '
                                                   'sleep 1; head -c 3000000 /dev/urandom > "$PIP_CACHE_DIR/new.whl"; echo wrote'))
    assert "/cache/pip" in first["logs"] and "10001" in first["logs"] and "wrote" in first["logs"]
    volume = client.volumes.get("ho-cache-proj-a-pip")
    assert volume.attrs["Labels"]["ho.kind"] == "cache" and volume.attrs["Labels"]["ho.project"] == "proj-a"

    second = api.run(cached_runner(api, "T-970002", 'ls "$PIP_CACHE_DIR"'))
    assert "old.whl" in second["logs"] and "new.whl" in second["logs"]  # another task of the same project reuses it
    other = api.run(cached_runner(api, "T-970003", 'ls "$PIP_CACHE_DIR" | wc -l', project="proj-b"))
    assert other["logs"].strip().splitlines()[-1] == "0"  # never shared with another project

    for container in client.containers.list(all=True, filters={"volume": "ho-cache-proj-a-pip"}):
        container.remove(force=True)  # finished workers are removed when the control plane finalizes them
    in_use = api.create(cached_runner(api, "T-970005", "sleep 30")).json()
    assert {c["volume"]: c for c in ops.caches()["caches"]}["ho-cache-proj-a-pip"]["in_use"]
    assert ops.maintain_caches()["trimmed"] == []  # never trimmed while a worker uses it
    ops.remove(in_use["execution"])

    ops.platform["machine"]["dependency_cache"] = {"enabled": True, "max_gb_per_cache": 0.004}  # about 4 MB
    listing = {c["volume"]: c for c in ops.caches()["caches"]}
    assert listing["ho-cache-proj-a-pip"]["bytes"] >= 6_000_000
    trimmed = {t["volume"]: t for t in ops.maintain_caches()["trimmed"]}
    assert trimmed["ho-cache-proj-a-pip"]["after"] <= 4.3 * 1024**2
    remaining = api.run(cached_runner(api, "T-970004", 'ls "$PIP_CACHE_DIR"'))
    assert "new.whl" in remaining["logs"] and "old.whl" not in remaining["logs"]  # least recently used went first
    for container in client.containers.list(all=True, filters={"volume": "ho-cache-proj-a-pip"}):
        container.remove(force=True)
    for container in client.containers.list(all=True, filters={"volume": "ho-cache-proj-b-pip"}):
        container.remove(force=True)

    assert ops.clear_cache("proj-a", "pip") == {"removed": ["ho-cache-proj-a-pip"]}
    ops.clear_cache("proj-b")
