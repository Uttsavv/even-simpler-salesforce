"""Polling of long-running jobs: delays from an :class:`~even_simpler_salesforce.Backoff`
within a total timeout. No I/O; the caller sleeps.

Bulk jobs, deployments, retrieves and asynchronous report runs all wait the same way:
``poll=esf.Backoff(...)`` and ``timeout=`` seconds, :data:`DEFAULT_TIMEOUT` by default.
"""

from __future__ import annotations

import time

from ..config import Backoff

__all__ = ["DEFAULT_TIMEOUT", "PollClock"]

DEFAULT_TIMEOUT = 3600.0
"""Default seconds a long-running job is waited for before the client gives up."""


class PollClock:
    """Delays between status calls of one wait.

    Args:
        timeout: Seconds to wait in total. Must be positive.
        poll: Delays between status calls. Defaults to ``Backoff()`` (2 s, doubling up to 30 s).

    Raises:
        ValueError: ``timeout`` is not positive.
    """

    __slots__ = ("_attempt", "_poll", "_slept", "_started", "timeout")

    def __init__(self, timeout: float, poll: Backoff | None = None) -> None:
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        self.timeout = timeout
        self._poll = poll if poll is not None else Backoff()
        self._started = time.monotonic()
        self._slept = 0.0
        self._attempt = 0

    def next_delay(self) -> float | None:
        """The next delay, cut to the time left, or ``None`` once ``timeout`` has passed.

        Time counts as passed when either the wall clock or the sum of the delays handed out
        reaches ``timeout``, so a fake sleep still ends the wait.
        """
        left = self.timeout - max(time.monotonic() - self._started, self._slept)
        if left <= 0:
            return None
        delay = min(self._poll.delay(self._attempt), left)
        self._attempt += 1
        self._slept += delay
        return delay
