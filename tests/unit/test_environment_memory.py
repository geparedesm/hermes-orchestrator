"""Memory for a task's test services: its own services, still up, do not count against their replacement (T-22)."""
from types import SimpleNamespace
from unittest.mock import Mock

from agent_manager.docker_ops import DockerOps

GIB = 1024**3


def container(memory: int, project: str | None = None) -> SimpleNamespace:
    labels = {"ho.managed": "true", **({"com.docker.compose.project": project} if project else {})}
    return SimpleNamespace(attrs={"HostConfig": {"Memory": memory}}, labels=labels)


def setup_ops(running: list[SimpleNamespace], total: int = 8 * GIB) -> DockerOps:
    client = Mock()

    def listing(filters=None, **_):
        wanted = (filters or {}).get("label") or []
        wanted = [wanted] if isinstance(wanted, str) else wanted
        return [c for c in running if all(c.labels.get(k) == v for k, v in (w.split("=", 1) for w in wanted))]

    client.containers.list.side_effect = listing
    client.info.return_value = {"MemTotal": total}
    ops = object.__new__(DockerOps)
    ops.client = client
    ops.platform = {"machine": {"docker_memory_reserve_gb": 2, "max_agent_workers": 2}}
    ops._running_workers = lambda: []
    return ops


def test_the_tasks_own_services_are_not_counted_against_their_replacement():
    # 8 GiB - 2 GiB reserve - (2 GiB worker + 1 GiB other task + 1 GiB own services) leaves 2 GiB; the own 1 GiB is back.
    ops = setup_ops([container(2 * GIB), container(GIB, "ho-t-21-shop"), container(GIB, "ho-t-22-shop")])
    assert ops._memory_for_environment("ho-t-22-shop") == 3 * GIB
    assert ops._memory_for_environment("ho-t-23-shop") == 2 * GIB


def test_memory_is_never_negative():
    ops = setup_ops([container(7 * GIB), container(GIB, "ho-t-22-shop")])
    assert ops._memory_for_environment("ho-t-23-shop") == 0
    assert ops._memory_for_environment("ho-t-22-shop") == 0
