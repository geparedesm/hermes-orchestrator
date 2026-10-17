"""Redis coordination (DATA_MODEL.md section 6).

Redis is never authoritative. Every call here is best effort: when Redis is
unavailable the scheduler falls back to polling PostgreSQL and live event
fan-out is skipped. Nothing is lost because PostgreSQL holds the state.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import redis

log = logging.getLogger(__name__)

WAKE_STREAM = "ho:wake:scheduler"
MAX_BLOCK_SECONDS = 5


class Coordinator:
    def __init__(self, url: str | None) -> None:
        self._client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2) if url else None
        # Blocking reads: the socket must outlast the block (a shorter one turns every wait into a timeout error and
        # wake-ups never arrive), and the block is short so that shutdown and a stalled Redis never wait long.
        self._blocking = (redis.Redis.from_url(url, socket_timeout=MAX_BLOCK_SECONDS + 2, socket_connect_timeout=2)
                          if url else None)
        self._last_id = "$"

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def ping(self) -> bool:
        if not self._client:
            return False
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False

    def wake_scheduler(self, reason: str) -> None:
        if not self._client:
            return
        try:
            self._client.xadd(WAKE_STREAM, {"reason": reason}, maxlen=1000, approximate=True)
        except redis.RedisError as exc:
            log.warning("scheduler wake-up not sent: %s", exc)

    def wait_for_wake(self, timeout_seconds: float, stop: threading.Event | None = None) -> bool:
        """Block until a wake-up arrives or the timeout expires (or `stop` is set). Returns True if woken.

        Reads in short blocks so that shutdown and a stalled Redis never wait long."""
        if not self._blocking:
            return False
        deadline = time.monotonic() + timeout_seconds
        if self._last_id == "$":
            # A concrete position: "$" means "newest at the time of each read", so a wake-up sent between two
            # reads would be skipped.
            try:
                newest = self._blocking.xrevrange(WAKE_STREAM, count=1)
            except redis.RedisError as exc:
                log.warning("scheduler wake-up wait failed; polling instead: %s", exc)
                return False
            self._last_id = newest[0][0] if newest else "0-0"
        while not (stop is not None and stop.is_set()):
            block_ms = int(min(deadline - time.monotonic(), MAX_BLOCK_SECONDS) * 1000)
            if block_ms <= 0:  # never 0: Redis reads that as "block forever"
                return False
            try:
                result = self._blocking.xread({WAKE_STREAM: self._last_id}, block=block_ms, count=100)
            except redis.RedisError as exc:
                log.warning("scheduler wake-up wait failed; polling instead: %s", exc)
                return False
            if result:
                self._last_id = result[0][1][-1][0]
                return True
        return False

    def publish_events(self, events: list[dict[str, Any]]) -> None:
        if not self._client or not events:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            for event in events:
                channel = f"ho:events:task:{event['task_id']}" if event.get("task_id") else "ho:events:platform"
                pipe.publish(channel, json.dumps(event))
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("event fan-out skipped: %s", exc)
