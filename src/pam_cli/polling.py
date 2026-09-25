"""A tiny polling helper shared by the login flow and the accounts grid."""

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def poll_until(
    check: Callable[[], T | None], timeout_ms: int, poll_interval: float = 0.5
) -> T | None:
    """Call check() until it returns something other than None (0 counts),
    or return None after timeout_ms."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        result = check()
        if result is not None:
            return result
        time.sleep(poll_interval)
    return None
