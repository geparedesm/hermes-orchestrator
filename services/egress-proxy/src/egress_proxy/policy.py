"""Egress decisions (NETWORK_MODEL.md section 5). Pure functions, unit-tested."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

MODES = ("PROVIDER_ONLY", "ALLOWLIST", "STANDARD")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Names that point at the host, the Docker VM, or cloud metadata.
_BLOCKED_NAMES = {
    "localhost",
    "host.docker.internal",
    "gateway.docker.internal",
    "kubernetes.docker.internal",
    "metadata.google.internal",
}
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".home.arpa")


@dataclass(frozen=True)
class Policy:
    mode: str
    allowed_domains: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (443,)
    denied_domains: tuple[str, ...] = ()  # for example production hosts

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown egress mode {self.mode!r}")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


def _matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern


def check_host(policy: Policy, host: str, port: int) -> Verdict:
    """Decide on the requested name before any DNS resolution."""
    host = host.lower().rstrip(".")
    if port not in policy.allowed_ports:
        return Verdict(False, f"port {port} not allowed")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return Verdict(False, "IP literals are not allowed; use a hostname")
    except ValueError:
        pass
    if not _HOSTNAME.match(host):
        return Verdict(False, "invalid hostname")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        return Verdict(False, "host-internal name")
    if any(_matches(host, d) for d in policy.denied_domains):
        return Verdict(False, "destination denied by project policy")
    if policy.mode == "STANDARD":
        return Verdict(True, "standard egress")
    if any(_matches(host, d) for d in policy.allowed_domains):
        return Verdict(True, "allowlisted")
    return Verdict(False, f"not in {policy.mode} allowlist")


def check_address(address: str) -> Verdict:
    """Decide on a resolved address; only globally routable unicast is allowed."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private:
        return Verdict(False, f"resolved to non-public address {ip}")
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return Verdict(False, f"resolved to shared address space {ip}")
    return Verdict(True, "public address")


def parse_domains(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(sorted({d.strip().lower() for d in value.split(",") if d.strip()}))
