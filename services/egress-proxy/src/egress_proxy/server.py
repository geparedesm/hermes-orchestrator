"""Minimal HTTPS CONNECT proxy, one instance per agent execution.

Only `CONNECT host:443` is supported: the hostname is checked against the
execution's policy, resolved here, every resolved address must be public, and
the connection goes to the address that was checked (no second resolution, so
DNS rebinding cannot redirect it). Every decision is logged as one JSON line.

Configuration (environment):
  HO_EGRESS_MODE        PROVIDER_ONLY | ALLOWLIST | STANDARD
  HO_ALLOWED_DOMAINS    comma-separated hostnames or *.suffix patterns
  HO_DENIED_DOMAINS     comma-separated patterns that are always denied
  HO_EXECUTION          execution id, included in logs
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from typing import Any

from .policy import Policy, check_address, check_host, parse_domains

LISTEN_PORT = 3128
MAX_HEADER_BYTES = 8192
IDLE_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 15
MAX_CONNECTIONS = 64


def log(**fields: Any) -> None:
    fields.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


class Proxy:
    def __init__(self, policy: Policy, execution: str) -> None:
        self.policy = policy
        self.execution = execution
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self.slots:
            try:
                await self._handle(reader, writer)
            except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass
            finally:
                writer.close()

    async def _reply(self, writer: asyncio.StreamWriter, status: str) -> None:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        if len(head) > MAX_HEADER_BYTES:
            return await self._reply(writer, "431 Request Header Fields Too Large")
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3 or parts[0] != "CONNECT":
            log(event="EGRESS_DENIED", execution=self.execution, request=request_line[:200], reason="only CONNECT is supported")
            return await self._reply(writer, "405 Method Not Allowed")
        host, _, port_text = parts[1].rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            return await self._reply(writer, "400 Bad Request")

        verdict = check_host(self.policy, host, port)
        if not verdict.allowed:
            log(event="EGRESS_DENIED", execution=self.execution, host=host, port=port, reason=verdict.reason)
            return await self._reply(writer, "403 Forbidden")

        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            log(event="EGRESS_DENIED", execution=self.execution, host=host, port=port, reason="name does not resolve")
            return await self._reply(writer, "502 Bad Gateway")
        addresses = sorted({info[4][0] for info in infos})
        for address in addresses:
            address_verdict = check_address(address)
            if not address_verdict.allowed:
                # One non-public answer rejects the name: it may be a rebinding attempt.
                log(event="EGRESS_DENIED", execution=self.execution, host=host, port=port, reason=address_verdict.reason)
                return await self._reply(writer, "403 Forbidden")

        started = time.monotonic()
        upstream_reader = upstream_writer = None
        connected_ip = None
        for address in addresses:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT_SECONDS
                )
                connected_ip = address
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream_writer is None:
            log(event="EGRESS_FAILED", execution=self.execution, host=host, port=port, reason="upstream unreachable")
            return await self._reply(writer, "502 Bad Gateway")

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        counters = {"up": 0, "down": 0}

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, key: str) -> None:
            try:
                while data := await asyncio.wait_for(src.read(65536), timeout=IDLE_TIMEOUT_SECONDS):
                    counters[key] += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.TimeoutError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, upstream_writer, "up"), pipe(upstream_reader, writer, "down"))
        log(event="EGRESS_ALLOWED", execution=self.execution, host=host, port=port, ip=connected_ip,
            reason=verdict.reason, bytes_up=counters["up"], bytes_down=counters["down"],
            seconds=round(time.monotonic() - started, 3))


async def serve(policy: Policy, execution: str) -> None:
    proxy = Proxy(policy, execution)
    server = await asyncio.start_server(proxy.handle, host="0.0.0.0", port=LISTEN_PORT)
    log(event="EGRESS_PROXY_STARTED", execution=execution, mode=policy.mode, allowed_domains=list(policy.allowed_domains))
    async with server:
        await server.serve_forever()


def main() -> None:
    policy = Policy(
        mode=os.environ.get("HO_EGRESS_MODE", "PROVIDER_ONLY"),
        allowed_domains=parse_domains(os.environ.get("HO_ALLOWED_DOMAINS")),
        denied_domains=parse_domains(os.environ.get("HO_DENIED_DOMAINS")),
    )
    asyncio.run(serve(policy, os.environ.get("HO_EXECUTION", "unknown")))


if __name__ == "__main__":
    main()
