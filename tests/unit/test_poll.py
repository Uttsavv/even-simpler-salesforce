"""Polling delays of long-running jobs (``core/poll.py``). No network, no sleeping."""

from __future__ import annotations

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import poll
from even_simpler_salesforce.core.poll import DEFAULT_TIMEOUT, PollClock


def test_default_timeout_is_an_hour() -> None:
    assert DEFAULT_TIMEOUT == 3600.0


def test_default_poll_is_backoff() -> None:
    clock = PollClock(100)
    assert [clock.next_delay() for _ in range(6)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_delays_stop_at_the_timeout() -> None:
    clock = PollClock(10, esf.Backoff(start=3, max=3))
    assert [clock.next_delay() for _ in range(5)] == [3.0, 3.0, 3.0, 1.0, None]
    assert clock.timeout == 10


def test_wall_clock_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(poll.time, "monotonic", lambda: now[0])
    clock = PollClock(10)
    assert clock.next_delay() == 2.0
    now[0] += 9.5  # a slow status call
    assert clock.next_delay() == pytest.approx(0.5)
    now[0] += 1
    assert clock.next_delay() is None


@pytest.mark.parametrize("timeout", [0, -1, float("nan")])
def test_rejects_a_bad_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        PollClock(timeout)
