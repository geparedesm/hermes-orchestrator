"""Scheduler wake-ups through Redis."""

from __future__ import annotations

import threading
import time

import pytest

from conftest import REDIS_URL  # type: ignore[import-not-found]
from control_plane.coordination import Coordinator

pytestmark = pytest.mark.integration


def test_a_wait_longer_than_the_command_timeout_still_receives_wake_ups():
    waiter, sender = Coordinator(REDIS_URL), Coordinator(REDIS_URL)
    # After the 2 s command timeout and after the first 5 s read block: the whole interval listens.
    threading.Timer(7, sender.wake_scheduler, args=("test",)).start()
    started = time.monotonic()
    assert waiter.wait_for_wake(12) is True
    assert 6.5 <= time.monotonic() - started < 11


def test_a_stopped_wait_returns_promptly():
    stop = threading.Event()
    threading.Timer(1, stop.set).start()
    started = time.monotonic()
    assert Coordinator(REDIS_URL).wait_for_wake(60, stop) is False
    assert time.monotonic() - started < 7  # within one read block
