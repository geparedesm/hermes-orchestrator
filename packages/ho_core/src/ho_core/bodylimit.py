"""ASGI middleware that bounds request bodies while they arrive, before anything parses them.

Counts the bytes actually received (Content-Length is only an early refusal), answers 413 past the limit, and
lets only a few large requests (task creation with attachments, executions carrying them) be read at a time.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class TooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app: Any, *, default: int, large: dict[tuple[str, str], int], concurrent_large: int = 2) -> None:
        self.app = app
        self.default = default
        self.large = large  # (method, path) -> limit
        self._slots = asyncio.Semaphore(concurrent_large)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit = self.large.get((scope["method"], scope["path"]))
        if limit is None:
            await self._limited(scope, receive, send, self.default)
            return
        async with self._slots:
            await self._limited(scope, receive, send, limit)

    async def _limited(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _refuse(send, limit)
            return
        received = 0
        exceeded = refused = False

        async def counted() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise TooLarge
            return message

        async def tracked(message: Message) -> None:
            # Frameworks turn errors while reading the body into their own response (FastAPI: 400); past the
            # limit the answer is 413 whatever the application made of it.
            nonlocal refused
            if not exceeded:
                await send(message)
            elif not refused and message["type"] == "http.response.start":
                refused = True
                await _refuse(send, limit)

        try:
            await self.app(scope, counted, tracked)
        except TooLarge:
            if not refused:
                refused = True
                await _refuse(send, limit)


async def _refuse(send: Send, limit: int) -> None:
    body = json.dumps({"error": "payload_too_large", "message": f"request body larger than {limit // 1024 // 1024} MiB"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
