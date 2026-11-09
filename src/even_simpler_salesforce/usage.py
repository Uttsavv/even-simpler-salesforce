"""API usage tracking, budget thresholds and the budget guard (§10). No network I/O."""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import Budget
from .core.limits import LimitInfo
from .errors import BudgetExceeded
from .models import UsageSnapshot

__all__ = ["UsageTracker"]

_WINDOW = timedelta(hours=1)
_MIN_SPAN = timedelta(minutes=1)

Emit = Callable[[str, dict[str, Any]], object]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UsageTracker:
    """Per-client-family usage counters. Thread-safe; shared by child clients.

    Exposed as ``sf.usage``.
    """

    def __init__(self, budget: Budget | None = None, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self.budget = budget
        self._clock = clock
        self._lock = threading.Lock()
        self._info: LimitInfo | None = None
        self._as_of: datetime | None = None
        self._samples: deque[tuple[datetime, int]] = deque()
        self._ops: Counter[str] = Counter()
        self._sobjects: Counter[str] = Counter()
        self._callers: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._retries = 0
        self._refreshes = 0
        self._warned: set[tuple[date, float]] = set()
        self._emit: Emit | None = None

    def bind(self, emit: Emit) -> None:
        self._emit = emit

    # --- recording -----------------------------------------------------------------

    def observe(self, info: LimitInfo) -> None:
        """Record a parsed ``Sforce-Limit-Info`` header."""
        now = self._clock()
        crossed: list[float] = []
        with self._lock:
            prev = self._info
            self._info = LimitInfo(
                info.api_used if info.api_used is not None else (prev.api_used if prev else None),
                info.api_max if info.api_max is not None else (prev.api_max if prev else None),
                info.bursts_used,
                info.bursts_max,
            )
            self._as_of = now
            if info.api_used is not None:
                self._samples.append((now, info.api_used))
                while self._samples and now - self._samples[0][0] > _WINDOW:
                    self._samples.popleft()
            frac = self._fraction_locked()
            if self.budget and frac is not None:
                day = now.date()
                for t in sorted(self.budget.warn_at):
                    if frac >= t and (day, t) not in self._warned:
                        self._warned.add((day, t))
                        crossed.append(t)
            used, mx = self._info.api_used, self._info.api_max
        if self._emit is not None:
            for t in crossed:
                self._emit("sf.usage.threshold", {"threshold": t, "fraction": frac, "used": used, "max": mx})

    def record_call(
        self,
        *,
        operation: str,
        sobject: str | None,
        caller: str | None,
        error_code: str | None,
        retries: int,
    ) -> None:
        with self._lock:
            self._ops[operation] += 1
            if sobject:
                self._sobjects[sobject] += 1
            self._callers[caller or "-"] += 1
            if error_code:
                self._errors[error_code] += 1
            self._retries += retries

    def record_refresh(self) -> None:
        with self._lock:
            self._refreshes += 1

    # --- reading -------------------------------------------------------------------

    def _fraction_locked(self) -> float | None:
        info = self._info
        if info is None or info.api_used is None or not info.api_max:
            return None
        return info.api_used / info.api_max

    @property
    def fraction(self) -> float | None:
        with self._lock:
            return self._fraction_locked()

    def _projection_locked(self) -> datetime | None:
        info = self._info
        if info is None or info.api_max is None or info.api_used is None or len(self._samples) < 2:
            return None
        (t0, u0), (t1, u1) = self._samples[0], self._samples[-1]
        span = t1 - t0
        if span < _MIN_SPAN or u1 <= u0:
            return None
        rate = (u1 - u0) / span.total_seconds()
        remaining = info.api_max - info.api_used
        if remaining <= 0:
            return t1
        return t1 + timedelta(seconds=remaining / rate)

    def snapshot(self) -> UsageSnapshot:
        """Current usage: org-wide header values plus per-process counters."""
        with self._lock:
            info = self._info or LimitInfo()
            return UsageSnapshot(
                api_used=info.api_used,
                api_max=info.api_max,
                api_fraction=self._fraction_locked(),
                as_of=self._as_of,
                calls_by_operation=dict(self._ops),
                calls_by_sobject=dict(self._sobjects),
                calls_by_caller=dict(self._callers),
                errors_by_code=dict(self._errors),
                retries=self._retries,
                refreshes=self._refreshes,
                projected_exhaustion=self._projection_locked(),
                api_bursts_used=info.bursts_used,
                api_bursts_max=info.bursts_max,
            )

    def latest(self) -> tuple[int | None, int | None]:
        with self._lock:
            info = self._info
            return (info.api_used, info.api_max) if info else (None, None)

    # --- guard ---------------------------------------------------------------------

    def check(self, priority: str, *, operation: str | None = None) -> None:
        """Raise :class:`BudgetExceeded` if the budget blocks a call with ``priority``."""
        b = self.budget
        if b is None:
            return
        frac = self.fraction
        if frac is None:
            return
        blocked = False
        if b.block_all_above is not None and frac > b.block_all_above and priority != "high":
            blocked = True
        elif b.pause_low_priority_above is not None and frac > b.pause_low_priority_above and priority == "low":
            blocked = True
        if blocked:
            raise BudgetExceeded(
                f"API budget: {frac:.1%} of the daily limit used; {priority}-priority call blocked",
                error_code="BUDGET_EXCEEDED",
                operation=operation,
                usage=self.snapshot(),
            )
