from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.limits import LimitInfo
from even_simpler_salesforce.usage import UsageTracker


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def tracker(budget: esf.Budget | None = None) -> tuple[UsageTracker, Clock, list[tuple[str, dict[str, Any]]]]:
    clock = Clock(T0)
    t = UsageTracker(budget, clock=clock)
    events: list[tuple[str, dict[str, Any]]] = []
    t.bind(lambda name, fields: events.append((name, fields)))
    return t, clock, events


def test_thresholds_fire_once_per_day() -> None:
    t, clock, events = tracker(esf.Budget(warn_at=(0.8, 0.5, 0.95)))
    t.observe(LimitInfo(40, 100))
    assert events == []
    t.observe(LimitInfo(85, 100))
    assert [e[1]["threshold"] for e in events] == [0.5, 0.8]
    assert events[0][0] == "sf.usage.threshold"
    assert events[1][1] == {"threshold": 0.8, "fraction": 0.85, "used": 85, "max": 100}
    t.observe(LimitInfo(90, 100))
    t.observe(LimitInfo(10, 100))
    t.observe(LimitInfo(90, 100))
    assert len(events) == 2  # no repeats on the same day
    t.observe(LimitInfo(96, 100))
    assert [e[1]["threshold"] for e in events] == [0.5, 0.8, 0.95]
    clock.now = T0 + timedelta(days=1)
    t.observe(LimitInfo(60, 100))
    assert [e[1]["threshold"] for e in events][3:] == [0.5]


def test_no_budget_no_events_and_partial_headers() -> None:
    t, _, events = tracker(None)
    t.observe(LimitInfo(99, 100))
    assert events == []
    assert t.fraction == 0.99
    t.observe(LimitInfo(None, None, 1, 750))  # bursts only: api values kept
    snap = t.snapshot()
    assert (snap.api_used, snap.api_max, snap.api_bursts_used, snap.api_bursts_max) == (99, 100, 1, 750)
    t2, _, _ = tracker()
    t2.observe(LimitInfo(None, None, 1, 2))
    assert t2.fraction is None
    assert t2.latest() == (None, None)
    t3, _, _ = tracker()
    t3.observe(LimitInfo(5, 0))
    assert t3.fraction is None


def test_unbound_tracker_does_not_emit() -> None:
    t = UsageTracker(esf.Budget(warn_at=(0.1,)))
    t.observe(LimitInfo(50, 100))
    assert t.fraction == 0.5


def test_projection() -> None:
    t, clock, _ = tracker()
    assert t.snapshot().projected_exhaustion is None
    t.observe(LimitInfo(1000, 101000))
    assert t.snapshot().projected_exhaustion is None  # one sample
    clock.now = T0 + timedelta(seconds=30)
    t.observe(LimitInfo(1100, 101000))
    assert t.snapshot().projected_exhaustion is None  # span < 1 min
    clock.now = T0 + timedelta(minutes=10)
    t.observe(LimitInfo(2000, 101000))
    # 1000 calls in 10 minutes → 100/min; 99,000 remaining → 990 minutes after the last sample.
    assert t.snapshot().projected_exhaustion == clock.now + timedelta(minutes=990)
    # Samples older than an hour are dropped from the window.
    clock.now = T0 + timedelta(hours=2)
    t.observe(LimitInfo(2000, 101000))
    assert t.snapshot().projected_exhaustion is None
    clock.now = T0 + timedelta(hours=2, minutes=5)
    t.observe(LimitInfo(1900, 101000))  # usage decreased (new day / reset): no projection
    assert t.snapshot().projected_exhaustion is None


def test_projection_exhausted() -> None:
    t, clock, _ = tracker()
    t.observe(LimitInfo(90, 100))
    clock.now = T0 + timedelta(minutes=5)
    t.observe(LimitInfo(100, 100))
    assert t.snapshot().projected_exhaustion == clock.now


def test_counters_and_snapshot() -> None:
    t, _, _ = tracker()
    t.record_call(operation="query", sobject="Lead", caller="job:a", error_code=None, retries=2)
    t.record_call(operation="query", sobject=None, caller=None, error_code="INVALID_FIELD", retries=0)
    t.record_refresh()
    t.observe(LimitInfo(10, 100))
    s = t.snapshot()
    assert isinstance(s, esf.UsageSnapshot)
    assert s.calls_by_operation == {"query": 2}
    assert s.calls_by_sobject == {"Lead": 1}
    assert s.calls_by_caller == {"job:a": 1, "-": 1}
    assert s.errors_by_code == {"INVALID_FIELD": 1}
    assert (s.retries, s.refreshes, s.api_used, s.api_max, s.api_fraction) == (2, 1, 10, 100, 0.1)
    assert s.as_of == T0
    assert t.latest() == (10, 100)


@pytest.mark.parametrize(
    ("budget", "fraction", "blocked"),
    [
        (esf.Budget(), 0.79, set()),
        (esf.Budget(), 0.81, {"low"}),
        (esf.Budget(pause_low_priority_above=None), 0.99, set()),
        (esf.Budget(block_all_above=0.9), 0.85, {"low"}),
        (esf.Budget(block_all_above=0.9), 0.95, {"low", "normal"}),
        (esf.Budget(pause_low_priority_above=None, block_all_above=0.5), 0.6, {"low", "normal"}),
        (esf.Budget(), 0.8, set()),  # strictly "above"
    ],
)
def test_guard(budget: esf.Budget, fraction: float, blocked: set[str]) -> None:
    t, _, _ = tracker(budget)
    t.observe(LimitInfo(int(fraction * 1000), 1000))
    for prio in ("low", "normal", "high"):
        if prio in blocked:
            with pytest.raises(esf.BudgetExceeded) as ei:
                t.check(prio, operation="query")
            assert ei.value.usage is not None
            assert ei.value.usage.api_used == int(fraction * 1000)
            assert ei.value.error_code == "BUDGET_EXCEEDED"
            assert ei.value.operation == "query"
            assert isinstance(ei.value, esf.LimitError)
        else:
            t.check(prio)


def test_guard_without_data_or_budget() -> None:
    t, _, _ = tracker(esf.Budget(block_all_above=0.0))
    t.check("low")  # no usage known yet
    t2, _, _ = tracker(None)
    t2.observe(LimitInfo(100, 100))
    t2.check("low")


# --- MemoryCache ------------------------------------------------------------------------------


class MonoClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_memory_cache_ttl() -> None:
    clock = MonoClock()
    c = esf.MemoryCache(clock=clock)
    assert isinstance(c, esf.Cache)
    c.set("a", {"v": 1}, ttl=10)
    assert c.get("a") == {"v": 1}
    clock.t += 9.9
    assert c.get("a") == {"v": 1}
    clock.t += 0.1
    assert c.get("a") is None
    assert len(c) == 0
    assert c.get("missing") is None


def test_memory_cache_lru() -> None:
    c = esf.MemoryCache(max_entries=2, clock=MonoClock())
    c.set("a", 1, 100)
    c.set("b", 2, 100)
    assert c.get("a") == 1  # a is now most recent
    c.set("c", 3, 100)  # evicts b
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3
    c.set("a", 10, 100)  # overwrite keeps size
    assert len(c) == 2
    assert c.get("a") == 10
    c.delete("a")
    c.delete("nope")
    assert c.get("a") is None
    c.clear()
    assert len(c) == 0


def test_memory_cache_default_clock() -> None:
    c = esf.MemoryCache()
    c.set("k", "v", 60)
    assert c.get("k") == "v"
